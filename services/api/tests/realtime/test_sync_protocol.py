"""Snapshot + delta consistency, resume and the gap -> resync path."""

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
from web3 import Web3

from app.matching.events import book_channel
from app.realtime import channels, publisher
from dev.ws_sync import ChannelSync, Outcome
from tests.conftest import Harness, SettingsFactory
from tests.realtime.kit import MID, Live, pub, seed_market
from tests.trading_kit import Chain, new_trader, post_quote, trading_settings
from tests.trading_kit import seed_market as seed_trading_market

PRICE = f"price:{MID}"
BOOK = book_channel(MID)


# ------------------------------------------------------------------------------ pure state machine
def _sync() -> ChannelSync[list[int], int]:
    return ChannelSync(lambda state, d: [*state, d])


def test_deltas_older_than_the_snapshot_are_dropped_and_newer_applied() -> None:
    s = _sync()
    for seq in (3, 4, 5, 6):  # buffered while the snapshot is in flight
        assert s.on_message(seq, seq) is Outcome.BUFFERED
    assert s.load_snapshot(4, [0]) is Outcome.APPLIED  # snapshot reflects up to seq 4
    assert s.state == [0, 5, 6] and s.last_seq == 6 and not s.syncing
    assert s.on_message(6, 6) is Outcome.DUPLICATE
    assert s.on_message(7, 7) is Outcome.APPLIED
    assert s.state == [0, 5, 6, 7]


def test_gap_while_live_returns_to_syncing_and_resync_recovers() -> None:
    s = _sync()
    s.load_snapshot(10, [])
    assert s.on_message(11, 11) is Outcome.APPLIED
    assert s.on_message(13, 13) is Outcome.GAP  # 12 never arrived
    assert s.syncing and s.gaps == 1
    assert s.on_message(14, 14) is Outcome.BUFFERED  # not applied on top of a hole
    assert s.state == [11]
    assert s.load_snapshot(13, [11, 12, 13]) is Outcome.APPLIED  # fresh snapshot covers the hole
    assert s.state == [11, 12, 13, 14] and s.last_seq == 14


def test_snapshot_older_than_the_buffer_needs_another_fetch() -> None:
    s = _sync()
    s.on_message(9, 9)
    assert s.load_snapshot(5, [0]) is Outcome.GAP  # 6..8 are in neither
    assert s.syncing


def test_resync_frame_discards_the_buffer() -> None:
    s = _sync()
    s.load_snapshot(1, [])
    s.on_resync()
    assert s.syncing and s.on_message(5, 5) is Outcome.BUFFERED


# ------------------------------------------------------------------------------------------ resume
@pytest.fixture
async def live(make_settings: SettingsFactory) -> AsyncIterator[Live]:
    async with Harness(make_settings()) as h:
        await seed_market(h)
        async with Live(h) as lv:
            yield lv


async def test_resume_replays_exactly_the_missed_messages_in_order(live: Live) -> None:
    async with live.connect() as c:
        await c.subscribe(PRICE)
        for i in range(3):
            await pub(live.h, PRICE, i=i)
        seen = [m["seq"] for m in await c.recv_data(3)]
    assert seen == [1, 2, 3]
    for i in range(3, 8):  # missed while disconnected
        await pub(live.h, PRICE, i=i)
    async with live.connect() as c:
        await c.send(op="subscribe", channels=[PRICE], resume={PRICE: 3}, id="r")
        replay = await c.recv_data(6)  # 5 replayed messages, then the `subscribed` frame
        assert [m["seq"] for m in replay[:5]] == [4, 5, 6, 7, 8 - 0]
        assert [m["data"]["i"] for m in replay[:5]] == [3, 4, 5, 6, 7]
        sub = replay[5]
        assert sub["type"] == "subscribed" and sub["channels"][0] == {
            "channel": PRICE,
            "seq": 8 - 0,
            "replayed": 5,
        }
        await pub(live.h, PRICE, i=8)
        nxt = await c.recv_type("price")
        assert nxt["seq"] == 9  # live continues with no duplicate and no gap


async def test_resume_at_head_replays_nothing(live: Live) -> None:
    await pub(live.h, PRICE, i=0)
    async with live.connect() as c:
        sub = await c.subscribe(PRICE, resume={PRICE: 1})
        assert sub["channels"][0]["replayed"] == 0


async def test_resume_outside_the_window_sends_resync(make_settings: SettingsFactory) -> None:
    async with Harness(make_settings(ws_retain_price_len=10)) as h:  # keeps the last 10 only
        await seed_market(h)
        for i in range(60):
            await pub(h, PRICE, i=i)
        async with Live(h) as lv, lv.connect() as c:
            await c.send(op="subscribe", channels=[PRICE], resume={PRICE: 2})
            rs = await c.recv_type("resync")
            assert (rs["channel"], rs["reason"], rs["seq"]) == (PRICE, "too_old", 60)
            assert (await c.recv_type("subscribed"))["channels"][0]["seq"] == 60
            await pub(h, PRICE, i=60)
            assert (await c.recv_type("price"))["seq"] == 61
            # inside the window it still resumes
            sub = await c.subscribe(f"forecast:{MID}")
            assert sub["channels"][0]["seq"] == 0


async def test_resume_ahead_of_head_after_a_redis_reset_sends_resync(live: Live) -> None:
    await pub(live.h, PRICE, i=0)
    async with live.connect() as c:
        await c.send(op="subscribe", channels=[PRICE], resume={PRICE: 999})
        rs = await c.recv_type("resync")
        assert (rs["reason"], rs["seq"]) == ("ahead_of_head", 1)


async def test_resume_with_too_many_missed_messages_sends_resync(
    make_settings: SettingsFactory,
) -> None:
    async with Harness(make_settings(ws_resume_max_messages=3)) as h:
        await seed_market(h)
        for i in range(10):
            await pub(h, PRICE, i=i)
        async with Live(h) as lv, lv.connect() as c:
            await c.send(op="subscribe", channels=[PRICE], resume={PRICE: 2})
            assert (await c.recv_type("resync"))["reason"] == "too_many"


async def test_resume_while_publishing_has_no_gap_and_no_duplicate(live: Live) -> None:
    """Live delivery and replay overlap: every seq from last+1 arrives exactly once, in order."""
    for i in range(20):
        await pub(live.h, PRICE, i=i)
    stop = asyncio.Event()

    async def producer() -> int:
        n = 20
        while not stop.is_set():
            await pub(live.h, PRICE, i=n)
            n += 1
            await asyncio.sleep(0)
        return n

    task = asyncio.create_task(producer())
    async with live.connect() as c:
        await c.send(op="subscribe", channels=[PRICE], resume={PRICE: 5}, id="r")
        seqs: list[int] = []
        sub: dict[str, Any] | None = None
        while len(seqs) < 120:
            m = await c.recv(10)
            if m["type"] == "subscribed":
                sub = m
            elif m["type"] == "price":
                seqs.append(m["seq"])
    stop.set()
    await task
    assert sub is not None
    assert seqs == list(range(6, 6 + len(seqs)))


# ------------------------------------------------------------------- gap -> REST resync (real book)
@pytest.mark.integration
async def test_lost_message_is_detected_and_the_book_resyncs_over_rest(
    make_settings: SettingsFactory, chain: dict[str, str], web3_sync: Web3
) -> None:
    """Orders flow through the real Phase 4 path. One orderbook message is lost in transit (its seq
    is allocated but never delivered); the client sees the gap, re-fetches /book and ends up with
    exactly the server's book."""
    async with Harness(trading_settings(make_settings, chain)) as h:
        await seed_trading_market(h)
        c = Chain(h, web3_sync)
        await post_quote(h, c, 150_000)
        seller = await new_trader(h, c, watt=10_000)
        redis = h.app.state.redis

        def apply(_: dict[str, Any], d: dict[str, Any]) -> dict[str, Any]:
            return {"bids": d["bids"], "asks": d["asks"]}  # orderbook deltas replace the state

        sync: ChannelSync[dict[str, Any], dict[str, Any]] = ChannelSync(apply)
        async with Live(h) as lv, httpx.AsyncClient(base_url=lv.http) as http:

            async def fetch() -> Outcome:
                r = await http.get(f"/api/v1/markets/{MID}/book")
                body = r.json()
                return sync.load_snapshot(
                    int(body["seq"]), {"bids": body["bids"], "asks": body["asks"]}
                )

            async with lv.connect() as ws:
                await ws.subscribe(BOOK)
                assert await fetch() is Outcome.APPLIED

                await seller.place_ok("sell", 1_000, 140_000)
                m1 = await ws.recv_type("orderbook")
                assert sync.on_message(m1["seq"], m1["data"]) is Outcome.APPLIED

                await redis.incr(channels.seq_key(BOOK))  # a message lost in transit
                await seller.place_ok("sell", 2_000, 145_000)
                m3 = await ws.recv_type("orderbook")
                assert m3["seq"] == m1["seq"] + 2
                assert sync.on_message(m3["seq"], m3["data"]) is Outcome.GAP

                assert await fetch() is Outcome.APPLIED  # re-snapshot over REST
                server = (await http.get(f"/api/v1/markets/{MID}/book")).json()
                assert sync.state == {"bids": server["bids"], "asks": server["asks"]}
                assert [lv_["price"] for lv_ in sync.state["asks"]] == ["140000", "145000"]
                assert sync.last_seq == server["seq"]

                await seller.place_ok("sell", 500, 150_000)
                m4 = await ws.recv_type("orderbook")
                assert sync.on_message(m4["seq"], m4["data"]) is Outcome.APPLIED
                assert len(sync.state["asks"]) == 3


async def test_snapshot_seq_is_read_before_the_data(live: Live) -> None:
    """/book returns the channel's head seq, and the seq only moves forward."""
    async with httpx.AsyncClient(base_url=live.http) as http:
        r0 = (await http.get(f"/api/v1/markets/{MID}/book")).json()
        assert r0["seq"] == 0
        await publisher.publish(live.h.app.state.redis, BOOK, "orderbook", {"x": 1})
        r1 = (await http.get(f"/api/v1/markets/{MID}/book")).json()
        assert r1["seq"] == 1
        assert json.dumps(r1["bids"]) == "[]"
