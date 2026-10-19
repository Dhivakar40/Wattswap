"""Gateway basics against a real server: subscribe, envelope, limits, heartbeat, backpressure."""

import asyncio
from collections.abc import AsyncIterator

import pytest

from app.realtime import channels
from tests.conftest import Harness, SettingsFactory
from tests.realtime.kit import MID, Live, pub, seed_market

PRICE = f"price:{MID}"
BOOK = f"orderbook:{MID}"


@pytest.fixture
async def live(make_settings: SettingsFactory) -> AsyncIterator[Live]:
    async with Harness(make_settings()) as h:
        await seed_market(h)
        async with Live(h) as lv:
            yield lv


async def test_subscribe_receive_unsubscribe(live: Live) -> None:
    async with live.connect() as c:
        sub = await c.subscribe(PRICE, BOOK)
        assert sub["ref"] == "s1"
        assert [x["channel"] for x in sub["channels"]] == [PRICE, BOOK]
        assert all(x["seq"] == 0 for x in sub["channels"])

        s1 = await pub(live.h, PRICE, x=1)
        msg = await c.recv_type("price")
        assert (msg["channel"], msg["seq"], msg["data"]) == (PRICE, s1, {"x": 1})
        assert set(msg) == {"type", "channel", "seq", "ts", "data"}

        await c.send(op="unsubscribe", channels=[PRICE], id="u1")
        un = await c.recv_type("unsubscribed")
        assert un["channels"] == [PRICE] and un["ref"] == "u1"
        await pub(live.h, PRICE, x=2)
        s3 = await pub(live.h, BOOK, "orderbook", y=1)
        got = await c.recv_type("orderbook")
        assert got["seq"] == s3 and got["channel"] == BOOK  # nothing from the left channel


async def test_head_seq_reported_and_messages_numbered(live: Live) -> None:
    for i in range(3):
        await pub(live.h, PRICE, i=i)
    async with live.connect() as c:
        sub = await c.subscribe(PRICE)
        assert sub["channels"][0]["seq"] == 3
        await pub(live.h, PRICE, i=3)
        assert (await c.recv_type("price"))["seq"] == 4


async def test_bad_and_unknown_channels_are_errors_not_closes(live: Live) -> None:
    async with live.connect() as c:
        await c.send(
            op="subscribe", channels=["nope", "price:0x12", f"price:0x{'cd' * 32}"], id="x"
        )
        errs = [await c.recv_type("error") for _ in range(3)]
        assert [e["code"] for e in errs] == ["bad_channel", "bad_channel", "unknown_market"]
        assert all(e["ref"] == "x" for e in errs)
        # the socket is still usable
        assert (await c.subscribe(PRICE))["channels"][0]["channel"] == PRICE


async def test_subscription_cap(make_settings: SettingsFactory) -> None:
    async with Harness(make_settings(ws_max_subscriptions=2)) as h:
        await seed_market(h)
        async with Live(h) as lv, lv.connect() as c:
            await c.send(op="subscribe", channels=[PRICE, BOOK, f"trades:{MID}"])
            err = await c.recv_type("error")
            assert err["code"] == "too_many_subscriptions" and err["channel"] == f"trades:{MID}"
            sub = await c.recv_type("subscribed")
            assert len(sub["channels"]) == 2


async def test_malformed_input_closes_4400(live: Live) -> None:
    for bad in [
        "not json",
        '{"op":"nope"}',
        '{"op":"subscribe","channels":[]}',
        '{"op":"ping","x":1}',
    ]:
        async with live.connect() as c:
            await c.ws.send(bad)
            assert await c.expect_close() == 4400


async def test_binary_frame_closes_4400(live: Live) -> None:
    async with live.connect() as c:
        await c.ws.send(b"\x00\x01")
        assert await c.expect_close() == 4400


async def test_oversize_frame_closes_4413(live: Live) -> None:
    async with live.connect() as c:
        await c.ws.send('{"op":"ping","id":"' + "a" * 5000 + '"}')
        assert await c.expect_close() == 4413


async def test_rate_limit_errors_then_close_4429(make_settings: SettingsFactory) -> None:
    settings = make_settings(ws_rate_burst=5, ws_rate_per_window=1, ws_rate_window_seconds=60.0)
    async with Harness(settings) as h, Live(h) as lv, lv.connect() as c:
        for _ in range(5):
            await c.send(op="ping")
        for _ in range(5):
            await c.recv_type("pong")
        await c.send(op="ping")
        assert (await c.recv_type("error"))["code"] == "rate_limited"
        await c.send(op="ping")
        await c.send(op="ping")
        assert await c.expect_close() == 4429


async def test_ping_op_gets_pong_with_ref(live: Live) -> None:
    async with live.connect() as c:
        await c.send(op="ping", id="p")
        assert (await c.recv_type("pong"))["ref"] == "p"


async def test_server_ping_and_heartbeat_timeout(make_settings: SettingsFactory) -> None:
    async with Harness(make_settings(ws_ping_seconds=0.3)) as h, Live(h) as lv:
        async with lv.connect() as c:  # answers: stays alive
            for _ in range(4):
                ping = await c.recv(2)
                assert ping["type"] == "ping"
                await c.send(op="pong")
        async with lv.connect() as silent:  # never sends anything
            assert await silent.expect_close(5) == 4408


async def test_slow_consumer_is_dropped_and_others_are_unaffected(
    make_settings: SettingsFactory,
) -> None:
    """The server-side writer of one socket stalls (as when a peer stops reading and the TCP
    buffers fill); its bounded queue overflows and only that socket is closed, with 4409."""
    settings = make_settings(ws_send_queue=8, ws_send_timeout_seconds=30.0)
    async with Harness(settings) as h:
        await seed_market(h)
        gw = h.app.state.gateway
        async with Live(h) as lv, lv.connect() as slow:
            await slow.subscribe(PRICE)
            (victim,) = gw.hub.connections

            async def stalled(_: str) -> None:
                await asyncio.sleep(3600)

            victim.ws.send_text = stalled
            async with lv.connect() as healthy:
                await healthy.subscribe(PRICE)
                for i in range(40):
                    await pub(h, PRICE, i=i)
                got = await healthy.recv_data(40, timeout=10)
                assert [m["seq"] for m in got] == list(range(1, 41))
                assert await slow.expect_close(10) == 4409


async def test_send_timeout_closes_4409(make_settings: SettingsFactory) -> None:
    async with Harness(make_settings(ws_send_timeout_seconds=0.3)) as h:
        await seed_market(h)
        async with Live(h) as lv, lv.connect() as c:
            await c.subscribe(PRICE)
            (victim,) = h.app.state.gateway.hub.connections

            async def stalled(_: str) -> None:
                await asyncio.sleep(3600)

            victim.ws.send_text = stalled
            await pub(h, PRICE, i=1)
            assert await c.expect_close(5) == 4409


async def test_connection_cap_per_ip(make_settings: SettingsFactory) -> None:
    async with Harness(make_settings(ws_max_conn_per_ip=2)) as h, Live(h) as lv:
        async with lv.connect() as a, lv.connect() as b, lv.connect() as c:
            assert await c.expect_close() == 4429
            await a.send(op="ping")
            await b.send(op="ping")
            assert (await a.recv_type("pong"))["type"] == "pong"
        await asyncio.sleep(0.3)  # slots are released on close
        async with lv.connect() as d:
            await d.send(op="ping")
            await d.recv_type("pong")


def test_retention_defaults_match_the_documented_windows() -> None:
    assert channels.retention_for(f"orderbook:{MID}") == channels.Retention(5000, 600)
    assert channels.retention_for(f"user:0x{'1' * 40}") == channels.Retention(1000, 1800)
    assert channels.retention_for(PRICE) == channels.Retention(500, 3600)
