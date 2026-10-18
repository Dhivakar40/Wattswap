"""The pricing service against real Postgres, Redis and Anvil, with Open-Meteo mocked by respx."""

import asyncio
import json
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import httpx
import pytest
import respx
from eth_account import Account
from sqlalchemy import func, select

from app.models import Market, PriceTick, PricingEvent
from app.pricing import publish, repo
from app.pricing.breaker import Mode
from app.pricing.model import load_multiplier, local_solar_hour
from app.pricing.params import DEFAULT_LOAD_PROFILE
from app.pricing.repo import DEFAULT_MARKET_ID
from tests.api.pricing_helpers import (
    FORECAST,
    FakeClock,
    make_service,
    mock_weather,
    owner_with_meter,
)
from tests.conftest import Harness, SettingsFactory
from tests.helpers import INTERVAL, past_start, post_reading, signed_reading

D = Decimal
MID = DEFAULT_MARKET_ID


async def rows(h: Harness) -> list[PriceTick]:
    async with h.app.state.sessionmaker() as s:
        return list((await s.scalars(select(PriceTick).order_by(PriceTick.ts))).all())


async def next_message(pubsub: Any, wait_s: float = 5.0) -> dict[str, Any] | None:
    """The next real pub/sub message (skips the subscribe confirmation)."""
    deadline = asyncio.get_running_loop().time() + wait_s
    while asyncio.get_running_loop().time() < deadline:
        msg = await pubsub.get_message(ignore_subscribe_messages=True, timeout=0.5)
        if msg is not None:
            return dict(msg)
    return None


async def events(h: Harness) -> list[str]:
    async with h.app.state.sessionmaker() as s:
        return [e.kind for e in await s.scalars(select(PricingEvent).order_by(PricingEvent.id))]


# ============================================================================ one tick, end to end
@respx.mock
async def test_a_tick_reaches_the_database_redis_pubsub_and_state(
    make_settings: SettingsFactory,
) -> None:
    async with Harness(make_settings()) as h:
        clock = FakeClock()
        route = mock_weather(clock)
        _, _, _, owner_key = await owner_with_meter(h)
        svc = make_service(h, clock)
        pubsub = h.app.state.redis.pubsub()
        await pubsub.subscribe(publish.channel(MID))

        assert await svc.poll_weather_once() == "ok"
        [t1] = await svc.tick_all()
        assert route.call_count == 1

        # --- the numbers: 5 kW at GHI 800 / 30 C -> 2.8288 kW; a brand-new owner holds no WATT
        d = t1.drivers
        assert d["supply"]["generation_kw"] == "2.828800"
        assert (
            d["supply"]["inventory_wh"] == 0 and d["supply"]["inventory_source"] == "held_balances"
        )
        assert d["weather"][0]["source"] == "live" and d["weather"][0]["age_s"] == 0
        hour = local_solar_hour(t1.market_time, 80_250_000)  # centre of the Chennai cell
        expected_load = load_multiplier(DEFAULT_LOAD_PROFILE, hour) * D("0.35") * 5
        assert D(d["demand"]["load_kw"]) == pytest.approx(expected_load, abs=D("0.000001"))
        assert t1.breaker.mode == Mode.NORMAL and t1.price == t1.raw_price

        # --- database
        [row] = await rows(h)
        assert int(row.price) == t1.price and row.market_id == MID and row.breaker_state == "NORMAL"
        assert row.drivers["supply"]["effective_kw"] == d["supply"]["effective_kw"]
        assert row.signature and row.signature.startswith("0x") and row.quote_timestamp
        assert row.clock_mode == "real" and row.market_time == t1.market_time

        # --- Redis snapshot + pub/sub, with a sequence number for gap detection
        latest = json.loads(await h.app.state.redis.get(publish.latest_key(MID)))
        assert (
            latest["type"] == "price" and latest["channel"] == f"price:{MID}" and latest["seq"] == 1
        )
        assert latest["data"]["price"] == t1.price and latest["data"]["quote"]["price"] == t1.price
        msg = await next_message(pubsub)
        assert msg is not None and json.loads(msg["data"]) == latest

        clock.advance(15)
        [t2] = await svc.tick_all()
        assert json.loads(await h.app.state.redis.get(publish.latest_key(MID)))["seq"] == 2
        msg2 = await next_message(pubsub)
        assert msg2 is not None and json.loads(msg2["data"])["seq"] == 2
        assert t2.ts == t1.ts + 15

        state = json.loads(await h.app.state.redis.get(publish.state_key(MID)))
        assert state["breaker"]["mode"] == "NORMAL" and state["weather"][0]["source"] == "live"
        assert owner_key  # the owner is a fresh wallet
        await pubsub.aclose()


@respx.mock
async def test_prices_and_money_are_integers_all_the_way_to_the_wire(
    make_settings: SettingsFactory,
) -> None:
    async with Harness(make_settings()) as h:
        clock = FakeClock()
        mock_weather(clock)
        await owner_with_meter(h)
        svc = make_service(h, clock)
        await svc.poll_weather_once()
        [t] = await svc.tick_all()
        wire = json.loads(await h.app.state.redis.get(publish.latest_key(MID)))["data"]

        def no_floats(x: object) -> None:
            assert not isinstance(x, float), x
            if isinstance(x, dict):
                for v in x.values():
                    no_floats(v)
            elif isinstance(x, list):
                for v in x:
                    no_floats(v)

        no_floats(wire)
        assert isinstance(wire["price"], int) and isinstance(wire["quote"]["price"], int)
        assert isinstance(wire["quote"]["timestamp"], int) and t.price == wire["price"]
        async with h.app.state.sessionmaker() as s:
            row = await s.scalar(select(PriceTick))
        assert row is not None and row.price == row.price.to_integral_value()


# ====================================================================== supply comes from the DB
@respx.mock
async def test_supply_reacts_when_meters_are_registered_or_deactivated(
    make_settings: SettingsFactory,
) -> None:
    async with Harness(make_settings()) as h:
        clock = FakeClock()
        mock_weather(clock)
        _, _, _, _ = await owner_with_meter(h, capacity_w=5000)
        svc = make_service(h, clock)
        await svc.poll_weather_once()
        [t1] = await svc.tick_all()
        assert t1.drivers["supply"]["generation_kw"] == "2.828800"

        # a second owner registers a 10 kW meter in the same cell: the NEXT tick sees 15 kW
        session2, meter2, _, _ = await owner_with_meter(h, capacity_w=10_000)
        clock.advance(15)
        [t2] = await svc.tick_all()
        assert t2.drivers["supply"]["generation_kw"] == "8.486400" and t2.supply_w > 2 * t1.supply_w
        assert (
            t2.drivers["weather"][0]["meters"] == 2
            and t2.drivers["weather"][0]["capacity_w"] == 15_000
        )
        # default model: demand scales with the installed base too, so the RAW price is unchanged
        assert (
            abs(t2.raw_price - t1.raw_price) * 1000 < t1.raw_price
        )  # <0.1 %: only the clock moved

        # deactivating it takes the supply away again
        r = await h.client.patch(
            f"/api/v1/meters/{meter2['id']}", json={"active": False}, headers=session2.headers
        )
        assert r.status_code == 200
        clock.advance(15)
        [t3] = await svc.tick_all()
        assert t3.drivers["supply"]["generation_kw"] == "2.828800"


@respx.mock
async def test_with_base_demand_added_supply_lowers_the_price(
    make_settings: SettingsFactory,
) -> None:
    async with Harness(make_settings()) as h:
        clock = FakeClock()
        mock_weather(clock)
        admin_key = "0x" + Account.create().key.hex()
        async with h.app.state.sessionmaker() as s:
            await repo.ensure_default_market(s)
            await repo.set_overrides(
                s,
                repo.GLOBAL_SCOPE,
                {"base_demand_kw": "3"},
                Account.from_key(admin_key).address.lower(),
                datetime.now(UTC),
            )
        await owner_with_meter(h, capacity_w=5000)
        svc = make_service(h, clock)
        await svc.poll_weather_once()
        [t1] = await svc.tick_all()
        await owner_with_meter(h, capacity_w=20_000)
        clock.advance(15)
        [t2] = await svc.tick_all()
        base1, base2 = D(t1.drivers["demand"]["base_kw"]), D(t2.drivers["demand"]["base_kw"])
        assert base1 > 0 and abs(base2 - base1) < D(
            "0.01"
        )  # unchanged by the new meter (clock drift only)
        assert t2.raw_price < t1.raw_price  # same consumers, five times the generation


@respx.mock
async def test_held_inventory_comes_from_the_chain_and_lowers_the_price(
    make_settings: SettingsFactory,
) -> None:
    """Mint real WATT to the owner through the meter pipeline and watch the supply term grow."""
    from tests.helpers import make_worker

    async with Harness(make_settings()) as h:
        clock = FakeClock()
        mock_weather(clock, ghi=0)  # night: generation 0, so inventory is the only supply
        _, meter, device_key, _ = await owner_with_meter(h)
        svc = make_service(h, clock)
        svc.settings = h.settings.model_copy(update={"pricing_inventory_refresh_seconds": 1})
        await svc.poll_weather_once()
        [before] = await svc.tick_all()
        assert before.drivers["supply"]["inventory_wh"] == 0
        assert before.drivers["clamps"][:1] == ["s_min"]  # no sun, no WATT: supply is the floor

        r = signed_reading(h, meter, device_key, past_start(30), 1500)
        assert (await post_reading(h, r)).status_code == 202
        assert await make_worker(h).run_once() == 1  # 1.500 WATT now on-chain for the owner

        clock.advance(2)  # past the 1 s balance cache
        [after] = await svc.tick_all()
        sup = after.drivers["supply"]
        assert sup["inventory_wh"] == 1500 and sup["inventory_source"] == "held_balances"
        # 1.5 kWh over 12 h = 0.125 kW, weighted by 0.5 = 0.0625 kW (below the 0.5 kW floor here)
        assert sup["inventory_kw"] == "0.125000" and sup["weighted_inventory_kw"] == "0.062500"
        assert "s_min" in after.drivers["clamps"]


@respx.mock
async def test_open_sell_orders_become_the_inventory_once_a_book_exists(
    make_settings: SettingsFactory,
) -> None:
    from app.models import Order, OrderSide, OrderStatus, User

    async with Harness(make_settings()) as h:
        clock = FakeClock()
        mock_weather(clock, ghi=0)
        _, _, _, owner_key = await owner_with_meter(h)
        svc = make_service(h, clock)
        await svc.poll_weather_once()
        async with h.app.state.sessionmaker() as s:
            await repo.ensure_default_market(s)
            user = await s.scalar(
                select(User).where(User.address == Account.from_key(owner_key).address.lower())
            )
            assert user is not None
            for side, amount, filled in (
                (OrderSide.SELL, 24_000, 4_000),
                (OrderSide.BUY, 6_000, 0),
            ):
                s.add(
                    Order(
                        order_hash="0x" + f"{amount:064x}",
                        maker_id=user.id,
                        maker=user.address,
                        market_id=MID,
                        side=side,
                        amount_wh=D(amount),
                        limit_price=D(150_000),
                        expiry=int(clock.t) + 3600,
                        nonce=D(1),
                        salt="0x" + "00" * 32,
                        signature="0x00",
                        status=OrderStatus.PARTIALLY_FILLED,
                        filled_wh=D(filled),
                    )
                )
            await s.commit()
        [t] = await svc.tick_all()
        assert t.drivers["supply"]["inventory_source"] == "open_sells"
        assert t.drivers["supply"]["inventory_wh"] == 20_000  # unfilled part of the sell order
        assert (
            t.drivers["demand"]["open_bid_wh"] == 6_000
            and t.drivers["demand"]["bids_kw"] == "6.000000"
        )


# ================================================================== weather per cell, not per meter
@respx.mock
async def test_weather_is_fetched_per_cell_in_one_request(make_settings: SettingsFactory) -> None:
    async with Harness(make_settings()) as h:
        clock = FakeClock()
        route = mock_weather(clock)
        for _ in range(4):  # four meters, one town: ONE cell
            await owner_with_meter(h, capacity_w=3000)
        await owner_with_meter(
            h, capacity_w=3000, lat=-33.8688, lon=151.2093
        )  # Sydney: a second cell
        svc = make_service(h, clock)
        assert await svc.poll_weather_once() == "ok"
        assert route.call_count == 1
        q = route.calls.last.request.url.params
        # cells are requested in a stable order (south to north): Sydney, then Chennai
        assert q["latitude"] == "-33.7500,13.2500" and q["longitude"] == "151.2500,80.2500"
        [t] = await svc.tick_all()
        cells = {w["cell"]: w for w in t.drivers["weather"]}
        assert set(cells) == {"26:160", "-68:302"}
        assert cells["26:160"]["meters"] == 4 and cells["26:160"]["capacity_w"] == 12_000
        assert cells["-68:302"]["meters"] == 1
        # polling again within the same hour is another single request, not one per meter
        clock.advance(600)
        await svc.poll_weather_once()
        assert route.call_count == 2


# ================================================================================= two markets
@respx.mock
async def test_markets_with_different_regions_price_independently(
    make_settings: SettingsFactory,
) -> None:
    async with Harness(make_settings(admin_addresses="")) as h:
        clock = FakeClock()
        mock_weather(clock)
        async with h.app.state.sessionmaker() as s:
            await repo.ensure_default_market(s)
            s.add(
                Market(
                    market_id=repo.market_id_for("WATT-SYDNEY"),
                    name="WATT-SYDNEY",
                    enabled=True,
                    min_lat_e6=-40_000_000,
                    max_lat_e6=-30_000_000,
                    min_lon_e6=140_000_000,
                    max_lon_e6=155_000_000,
                )
            )
            await s.commit()
        await owner_with_meter(h, capacity_w=5000)
        await owner_with_meter(h, capacity_w=8000, lat=-33.8688, lon=151.2093)
        svc = make_service(h, clock)
        await svc.poll_weather_once()
        ticks = {t.drivers["market_time"]: t for t in await svc.tick_all()}
        assert len(await rows(h)) == 2
        by_market: dict[str, PriceTick] = {r.market_id: r for r in await rows(h)}
        glob = by_market[MID]
        syd = by_market[repo.market_id_for("WATT-SYDNEY")]
        assert sorted(w["capacity_w"] for w in glob.drivers["weather"]) == [
            5000,
            8000,
        ]  # global: both
        assert [w["capacity_w"] for w in syd.drivers["weather"]] == [8000]  # Sydney only its own
        assert ticks  # both produced ticks; published on their own channels
        assert await h.app.state.redis.get(publish.latest_key(repo.market_id_for("WATT-SYDNEY")))


# ================================================================== Open-Meteo failure modes
@respx.mock
async def test_stale_weather_trips_the_breaker_then_recovers_with_hysteresis(
    make_settings: SettingsFactory,
) -> None:
    async with Harness(make_settings()) as h:
        clock = FakeClock()
        route = mock_weather(clock)
        await owner_with_meter(h)
        svc = make_service(h, clock)
        await svc.poll_weather_once()
        [healthy] = await svc.tick_all()
        assert healthy.breaker.mode == Mode.NORMAL

        # Open-Meteo goes down. The last good data is kept, and ages.
        route.mock(return_value=httpx.Response(503))
        clock.advance(600)
        assert await svc.poll_weather_once() == "failed"
        clock.advance(15)
        [aging] = await svc.tick_all()
        assert (
            aging.breaker.mode == Mode.NORMAL
            and aging.drivers["weather"][0]["source"] == "last_good"
        )
        assert aging.drivers["weather"][0]["age_s"] == 615  # 10 min old: tolerated

        clock.advance(1300)  # 31+ minutes since the last good fetch
        [frozen] = await svc.tick_all()
        assert frozen.breaker.mode == Mode.FROZEN and frozen.weather_stale
        assert frozen.price == aging.price  # held
        assert frozen.band_bps == 450  # widened hint (150 x 3), under the 500 on-chain band
        assert "weather stale" in frozen.drivers["breaker"]["stale_detail"]
        assert "breaker_frozen" in await events(h)
        st = json.loads(await h.app.state.redis.get(publish.state_key(MID)))
        assert st["breaker"]["mode"] == "FROZEN" and st["weatherStale"] is True
        assert "http_5xx" in str(st["weatherLastError"])

        # quotes keep being signed with a fresh timestamp while frozen (so the exchange accepts them)
        quote = json.loads(await h.app.state.redis.get(publish.quote_key(MID)))
        assert quote["price"] == frozen.price

        # Open-Meteo comes back
        mock_weather(clock)
        clock.advance(120)
        assert await svc.poll_weather_once() == "ok"
        modes = []
        for _ in range(9):
            clock.advance(15)
            [t] = await svc.tick_all()
            modes.append(t.breaker.mode)
            assert t.price == frozen.price or t.breaker.mode == Mode.NORMAL
        # recovery_ticks = 8: seven RECOVERING ticks (price still held), NORMAL from the eighth
        assert modes[:7] == [Mode.RECOVERING] * 7 and modes[7:] == [Mode.NORMAL] * 2
        kinds = await events(h)
        assert (
            kinds.count("breaker_frozen") == 1
            and "breaker_recovering" in kinds
            and "breaker_recovered" in kinds
        )


@respx.mock
async def test_timeouts_and_429s_degrade_gracefully(make_settings: SettingsFactory) -> None:
    async with Harness(make_settings()) as h:
        clock = FakeClock()
        route = mock_weather(clock)
        await owner_with_meter(h)
        svc = make_service(h, clock)
        await svc.poll_weather_once()
        base_calls = route.call_count

        route.mock(side_effect=httpx.ReadTimeout("slow"))
        clock.advance(600)
        assert await svc.poll_weather_once() == "failed"
        route.mock(return_value=httpx.Response(429, headers={"retry-after": "300"}))
        clock.advance(60)
        assert await svc.poll_weather_once() == "failed"  # backoff elapsed, 429 this time
        calls = route.call_count
        clock.advance(100)
        assert await svc.poll_weather_once() == "skipped"  # inside Retry-After: no request is made
        assert route.call_count == calls and calls > base_calls
        [t] = await svc.tick_all()  # pricing carries on with last-good weather
        assert t.breaker.mode == Mode.NORMAL and t.drivers["weather"][0]["source"] == "last_good"

        route.mock(
            return_value=httpx.Response(
                200, json={"hourly": {"time": [], "shortwave_radiation": [], "temperature_2m": []}}
            )
        )
        clock.advance(400)
        assert await svc.poll_weather_once() == "failed"  # malformed body = failure, data kept
        [t2] = await svc.tick_all()
        assert t2.drivers["weather"][0]["source"] == "last_good"


@respx.mock
async def test_a_market_that_never_got_weather_is_frozen_not_guessed(
    make_settings: SettingsFactory,
) -> None:
    async with Harness(make_settings()) as h:
        clock = FakeClock()
        respx.get(FORECAST).mock(side_effect=httpx.ConnectError("no network"))
        await owner_with_meter(h)
        svc = make_service(h, clock)
        assert await svc.poll_weather_once() == "failed"
        [t] = await svc.tick_all()
        assert t.breaker.mode == Mode.FROZEN and t.drivers["weather"][0]["source"] == "none"
        assert t.price == 150_000  # cold start: held at the neutral base price


# ================================================================================ demo clock
@respx.mock
async def test_demo_clock_follows_simulated_readings_but_quotes_keep_real_time(
    make_settings: SettingsFactory,
) -> None:
    async with Harness(make_settings(pricing_clock_mode="follow_readings")) as h:
        clock = FakeClock()
        mock_weather(clock)
        _, meter, device_key, _ = await owner_with_meter(h)
        svc = make_service(h, clock)

        # a simulated (accelerated) reading for an interval ~10 hours ago has just been ingested
        start = past_start(40)
        assert (
            await post_reading(h, signed_reading(h, meter, device_key, start, 400))
        ).status_code == 202
        [t] = await svc.tick_all()  # no live poll at all: the history path fetches that day
        sim_end = start + INTERVAL
        assert (
            t.market_time == sim_end and t.ts - t.market_time > 3600
        )  # market clock is in the past
        assert (
            t.drivers["weather"][0]["source"] == "archive" and t.drivers["weather"][0]["age_s"] == 0
        )
        expected_hour = local_solar_hour(sim_end, 80_250_000)
        assert D(t.drivers["local_solar_hour"]) == pytest.approx(expected_hour, abs=D("0.001"))
        async with h.app.state.sessionmaker() as s:
            row = await s.scalar(select(PriceTick).order_by(PriceTick.id.desc()))
        assert row is not None and row.clock_mode == "follow_readings"
        assert (
            row.quote_timestamp is not None and row.quote_timestamp >= int(clock.t) - 60
        )  # REAL time
        wire = json.loads(await h.app.state.redis.get(publish.latest_key(MID)))["data"]
        assert wire["accelerated"] is True and wire["marketTime"] == sim_end

        # a second reading moves the market clock again, so prices follow the simulated day
        assert (
            await post_reading(h, signed_reading(h, meter, device_key, start + INTERVAL, 400))
        ).status_code == 202
        clock.advance(15)
        [t2] = await svc.tick_all()
        assert t2.market_time == sim_end + INTERVAL

        # when the simulator stops, the engine returns to real time
        clock.advance(200)  # longer than the 120 s follow window
        [t3] = await svc.tick_all()
        assert t3.market_time == int(clock.t) and t3.drivers["market_time"] == int(clock.t)
        async with h.app.state.sessionmaker() as s:
            last = await s.scalar(select(PriceTick).order_by(PriceTick.id.desc()))
        assert last is not None and last.clock_mode == "real"


# ==================================================================== restart and leader election
@respx.mock
async def test_a_restarted_service_continues_from_the_persisted_state(
    make_settings: SettingsFactory,
) -> None:
    async with Harness(make_settings()) as h:
        clock = FakeClock()
        mock_weather(clock)
        await owner_with_meter(h)
        a = make_service(h, clock)
        await a.poll_weather_once()
        for _ in range(6):
            await a.tick_all()
            clock.advance(15)
        last = a.markets[MID].latest
        assert last is not None

        b = make_service(h, clock)  # a fresh process: no in-memory state at all
        await b.poll_weather_once()
        [t] = await b.tick_all()
        assert t.drivers["smoothing"]["previous"] == last.price  # restored, not re-seeded
        assert abs(t.price - last.price) * 10_000 <= last.price * 25
        assert len(await rows(h)) == 7


@respx.mock
async def test_only_one_instance_prices_at_a_time(make_settings: SettingsFactory) -> None:
    async with Harness(make_settings(pricing_tick_seconds=1)) as h:
        clock = FakeClock()
        mock_weather(clock)
        await owner_with_meter(h)
        a, b = make_service(h, clock), make_service(h, clock)
        stop = asyncio.Event()
        tasks = [asyncio.create_task(a.run(stop)), asyncio.create_task(b.run(stop))]
        try:
            for _ in range(60):
                await asyncio.sleep(0.25)
                clock.advance(1)  # distinct ts per tick
                if len(await rows(h)) >= 3:
                    break
        finally:
            stop.set()
            await asyncio.gather(*tasks)
        leaders = [svc for svc in (a, b) if svc.markets]
        assert len(leaders) == 1  # the other never ticked
        assert len(await rows(h)) >= 3


async def test_concurrent_start_up_creates_the_default_market_exactly_once(
    make_settings: SettingsFactory,
) -> None:
    """The poller and the ticker (and other processes) all ensure the market at start."""
    async with Harness(make_settings()) as h:

        async def ensure() -> str:
            async with h.app.state.sessionmaker() as s:
                return (await repo.ensure_default_market(s)).market_id

        ids = await asyncio.gather(*[ensure() for _ in range(12)])
        assert set(ids) == {MID}
        async with h.app.state.sessionmaker() as s:
            assert await s.scalar(select(func.count()).select_from(Market)) == 1


@respx.mock
async def test_a_starting_service_polls_weather_before_its_first_tick(
    make_settings: SettingsFactory,
) -> None:
    """Otherwise every restart would begin FROZEN ('no weather') and sit out the hysteresis."""
    async with Harness(make_settings(pricing_tick_seconds=15)) as h:
        clock = FakeClock()
        route = mock_weather(clock)
        await owner_with_meter(h)
        svc = make_service(h, clock)
        stop = asyncio.Event()
        task = asyncio.create_task(svc.run(stop))
        try:
            for _ in range(80):
                await asyncio.sleep(0.25)
                if await rows(h):
                    break
        finally:
            stop.set()
            await task
        [first, *_] = await rows(h)
        assert first.breaker_state == "NORMAL" and first.drivers["weather"][0]["source"] == "live"
        assert route.call_count == 1  # exactly one poll so far: the initial one, not a duplicate
