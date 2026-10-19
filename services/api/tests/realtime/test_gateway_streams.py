"""Real producers through the gateway: price drivers, forecast, meters, book and trade flow."""

import asyncio
from typing import Any

import pytest
import respx
from web3 import Web3

from app.matching.events import book_channel, publish_book
from app.models import SettlementStatus
from tests.api.pricing_helpers import FakeClock, make_service, mock_weather, owner_with_meter
from tests.conftest import KEY_ALICE, KEY_BOB, Harness, SettingsFactory
from tests.helpers import (
    INTERVAL,
    login,
    make_worker,
    new_meter,
    past_start,
    post_reading,
    signed_reading,
)
from tests.realtime.kit import MID, Live, seed_market
from tests.trading_kit import (
    Chain,
    build,
    new_trader,
    post_quote,
    settle_all,
    settlements,
    trading_settings,
)
from tests.trading_kit import seed_market as seed_trading_market

PRICE = f"price:{MID}"
FORECAST = f"forecast:{MID}"


# ------------------------------------------------------------------------- price drivers + forecast
@respx.mock
async def test_live_ticks_carry_the_price_drivers_and_the_forecast_is_pushed(
    make_settings: SettingsFactory,
) -> None:
    """The Market Insights page needs no polling: each tick message includes the drivers, and the
    hourly outlook arrives on its own channel when it is recomputed."""
    async with Harness(make_settings()) as h:
        clock = FakeClock()
        mock_weather(clock)
        await owner_with_meter(h)
        svc = make_service(h, clock)
        await svc.poll_weather_once()
        async with Live(h) as lv, lv.connect() as c:
            await c.subscribe(PRICE, FORECAST)
            [tick] = await svc.tick_all()
            got: dict[str, dict[str, Any]] = {}
            while len(got) < 2:
                m = await c.recv(5)
                if m["type"] in ("price", "forecast"):
                    got[m["type"]] = m
            price = got["price"]
            assert price["seq"] == 1 and price["channel"] == PRICE
            d = price["data"]
            assert d["price"] == tick.price and d["quote"]["price"] == tick.price
            drivers = d["drivers"]
            for key in ("supply", "demand", "weather", "breaker", "smoothing"):
                assert key in drivers, key
            assert drivers["supply"]["generation_kw"] == "2.828800"
            assert isinstance(d["events"], list) and d["breaker"] == "NORMAL"
            fc = got["forecast"]
            assert fc["channel"] == FORECAST and fc["data"]["marketId"] == MID
            assert len(fc["data"]["hours"]) > 0 and "supply_kw" in fc["data"]["hours"][0]


# ------------------------------------------------------------------------------------------ meters
async def test_meter_channel_is_owner_only_and_streams_reading_then_mint(
    make_settings: SettingsFactory,
) -> None:
    async with Harness(make_settings()) as h:
        await seed_market(h)
        alice = await login(h, KEY_ALICE)
        bob = await login(h, KEY_BOB)
        meter, key = await new_meter(h, alice)
        chan = f"meter:{meter['meter_id']}"
        async with Live(h) as lv:
            async with lv.connect() as anon:
                await anon.send(op="subscribe", channels=[chan])
                assert (await anon.recv_type("error"))["code"] == "unauthorized"
            async with lv.connect(cookie=bob.headers["cookie"]) as other:
                await other.send(op="subscribe", channels=[chan])
                assert (await other.recv_type("error"))["code"] == "forbidden"
            async with lv.connect(cookie=alice.headers["cookie"]) as owner:
                sub = await owner.subscribe(chan)
                assert sub["channels"][0]["seq"] == 0
                t = past_start(30)
                r = await post_reading(h, signed_reading(h, meter, key, t, 600))
                assert r.status_code == 202
                reading = await owner.recv_type("reading")
                assert reading["channel"] == chan and reading["seq"] == 1
                assert reading["data"]["amount_wh"] == 600 and reading["data"]["simulated"] is True
                assert await make_worker(h).run_once() == 1
                statuses: list[str] = []
                while not statuses or statuses[-1] != "confirmed":
                    m = await owner.recv_type("mint", 15)
                    assert m["data"]["simulated"] is True
                    statuses.append(m["data"]["status"])
                assert statuses == ["submitted", "confirmed"]
                # a duplicate post does not re-announce
                r2 = await post_reading(h, signed_reading(h, meter, key, t, 600))
                assert r2.status_code == 200
                await owner.send(op="ping")
                assert (await owner.recv_type("pong"))["type"] == "pong"
        assert INTERVAL == 900


# --------------------------------------------------------------------------------------- the book
async def test_concurrent_book_flushes_never_publish_an_older_book_under_a_newer_seq(
    make_settings: SettingsFactory,
) -> None:
    """Regression for compute-then-publish racing. A burst of changes (each flush reads the book
    after its own commit) must leave the LAST published book equal to the database, and the seqs
    on the wire must be gapless."""
    from app.matching.events import book_levels

    async with Harness(make_settings()) as h:
        await seed_market(h)
        redis, sm = h.app.state.redis, h.app.state.sessionmaker
        async with Live(h) as lv, lv.connect() as c:
            await c.subscribe(book_channel(MID))
            await asyncio.gather(*(publish_book(redis, sm, MID) for _ in range(40)))
            await asyncio.sleep(0.5)
            seqs: list[int] = []
            while True:
                try:
                    m = await c.recv(0.5)
                except TimeoutError:
                    break
                if m["type"] == "orderbook":
                    seqs.append(m["seq"])
            assert seqs == list(range(1, len(seqs) + 1)) and len(seqs) >= 1
            async with sm() as s:
                truth = (await book_levels(s, MID, 0)).model_dump(mode="json")
            assert m["data"]["bids"] == truth["bids"] if seqs else True


# ------------------------------------------------------------------------- trade flow, end to end
@pytest.mark.integration
async def test_crossing_orders_stream_order_fill_and_trade_in_seq_order(
    make_settings: SettingsFactory, chain: dict[str, str], web3_sync: Web3
) -> None:
    async with Harness(trading_settings(make_settings, chain)) as h:
        await seed_trading_market(h)
        c = Chain(h, web3_sync)
        await post_quote(h, c, 150_000)
        seller = await new_trader(h, c, watt=5_000)
        buyer = await new_trader(h, c, usdc=10_000_000)
        s_chan, b_chan = f"user:{seller.address.lower()}", f"user:{buyer.address.lower()}"
        async with Live(h) as lv:
            async with (
                lv.connect(cookie=seller.session.headers["cookie"]) as sw,
                lv.connect(cookie=buyer.session.headers["cookie"]) as bw,
                lv.connect() as tape,
            ):
                await sw.subscribe(s_chan)
                await bw.subscribe(b_chan)
                await tape.subscribe(f"trades:{MID}", book_channel(MID))

                await seller.place_ok("sell", 2_000, 140_000)
                await buyer.place_ok("buy", 2_000, 160_000)
                await settle_all(build(h), c)
                (st,) = await settlements(h)
                assert st.status == SettlementStatus.CONFIRMED

                async def stream(client: Any, want: str) -> list[dict[str, Any]]:
                    out: list[dict[str, Any]] = []
                    while True:
                        try:
                            m = await client.recv(1.5)
                        except TimeoutError:
                            return out
                        if m["type"] == want or want == "*":
                            out.append(m)

                s_msgs = await stream(sw, "*")
                b_msgs = await stream(bw, "*")
                tape_msgs = await stream(tape, "*")
                for msgs in (s_msgs, b_msgs):
                    seqs = [m["seq"] for m in msgs]
                    assert seqs == list(range(1, len(seqs) + 1))  # gapless per channel
                    kinds = [m["type"] for m in msgs if m["type"] != "ping"]
                    assert kinds[0] == "order" and "fill" in kinds
                # the maker only ever sees their own data
                assert {m["channel"] for m in s_msgs} == {s_chan}
                assert {m["channel"] for m in b_msgs} == {b_chan}
                fills = [m for m in s_msgs if m["type"] == "fill"]
                assert fills[-1]["data"]["status"] == "confirmed"
                assert fills[-1]["data"]["fill_wh"] == "2000"
                trades = [m for m in tape_msgs if m["type"] == "trade"]
                assert len(trades) == 1 and trades[0]["data"]["fill_wh"] == "2000"
                books = [m for m in tape_msgs if m["type"] == "orderbook"]
                assert books and all("bids" in m["data"] and "asks" in m["data"] for m in books)
                assert books[-1]["data"]["asks"] == [] and books[-1]["data"]["bids"] == []
                # no addresses on the public book
                assert "maker" not in str(books)
