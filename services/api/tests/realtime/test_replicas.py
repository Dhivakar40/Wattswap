"""Two API replicas on one Redis behave as one gateway; the hub survives losing its pub/sub link."""

import asyncio
from typing import Any

from tests.conftest import KEY_ALICE, Harness, SettingsFactory
from tests.helpers import login
from tests.realtime.kit import MID, Live, pub, seed_market

BOOK = f"orderbook:{MID}"


async def test_two_replicas_deliver_the_same_sequence_to_clients_on_each(
    make_settings: SettingsFactory,
) -> None:
    settings = make_settings()
    async with Harness(settings) as h1, Harness(settings) as h2:
        await seed_market(h1)
        async with Live(h1) as l1, Live(h2) as l2:
            async with l1.connect() as a, l2.connect() as b:
                await a.subscribe(BOOK)
                await b.subscribe(BOOK)
                sent: list[int] = []
                for i in range(30):  # publishers on both replicas, interleaved
                    sent.append(await pub(h1 if i % 2 else h2, BOOK, "orderbook", i=i))
                got_a = [m["seq"] for m in await a.recv_data(30)]
                got_b = [m["seq"] for m in await b.recv_data(30)]
                assert got_a == got_b == list(range(1, 31))
                assert sorted(sent) == list(range(1, 31))
                # each replica holds ONE redis subscription for the channel, however many clients
                assert h1.app.state.gateway.hub.channel_count() == 1


async def test_logout_on_one_replica_closes_the_socket_held_by_the_other(
    make_settings: SettingsFactory,
) -> None:
    settings = make_settings()
    async with Harness(settings) as h1, Harness(settings) as h2:
        async with Live(h1) as l1, Live(h2) as l2:
            alice = await login(h1, KEY_ALICE)
            async with l2.connect(cookie=alice.headers["cookie"]) as on_2:
                async with l1.connect(cookie=alice.headers["cookie"]) as on_1:
                    await on_1.send(op="ping")
                    await on_1.recv_type("pong")
                    await on_2.send(op="ping")
                    await on_2.recv_type("pong")
                    r = await h1.client.post("/api/v1/auth/logout", headers=alice.headers)
                    assert r.status_code == 204
                    assert await on_1.expect_close() == 4401
                    assert await on_2.expect_close() == 4401


async def test_hub_reconnects_after_losing_pubsub_and_catches_clients_up(
    make_settings: SettingsFactory,
) -> None:
    """Messages published while the hub had no Redis subscription are replayed from the log, in
    order and without duplicates, once it reconnects."""
    async with Harness(make_settings()) as h:
        await seed_market(h)
        redis = h.app.state.redis
        hub = h.app.state.gateway.hub
        async with Live(h) as lv, lv.connect() as c:
            await c.subscribe(BOOK)
            for i in range(3):
                await pub(h, BOOK, "orderbook", i=i)
            first = [m["seq"] for m in await c.recv_data(3)]
            assert first == [1, 2, 3]

            async def kill_pubsub() -> None:
                kwargs: dict[str, Any] = {"_type": "pubsub"}
                await redis.client_kill_filter(**kwargs)

            await kill_pubsub()
            for i in range(3, 9):
                await pub(h, BOOK, "orderbook", i=i)
                await asyncio.sleep(0.02)
            rest: list[int] = []
            while len(rest) < 6:
                m = await c.recv(10)
                if m["type"] == "orderbook":
                    rest.append(m["seq"])
            assert rest == [4, 5, 6, 7, 8, 9]
            assert hub.reconnects >= 1
            await pub(h, BOOK, "orderbook", i=9)  # and live delivery works again
            assert (await c.recv_type("orderbook"))["seq"] == 10
