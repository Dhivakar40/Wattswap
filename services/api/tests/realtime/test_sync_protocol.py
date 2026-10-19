"""Snapshot + delta consistency, resume, epochs and the gap/epoch -> resync paths."""

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
from web3 import Web3

from app.matching.events import book_channel, publish_book
from app.realtime import channels, publisher
from dev.ws_sync import ChannelSync, Outcome
from tests.conftest import Harness, SettingsFactory
from tests.realtime.kit import MID, Live, pub, seed_market
from tests.trading_kit import Chain, new_trader, post_quote, trading_settings
from tests.trading_kit import seed_market as seed_trading_market

PRICE = f"price:{MID}"
BOOK = book_channel(MID)
E1, E2 = "epoch-one", "epoch-two"


# ------------------------------------------------------------------------------ pure state machine
def _sync() -> ChannelSync[list[int], int]:
    return ChannelSync(lambda state, d: [*state, d])


def test_deltas_older_than_the_snapshot_are_dropped_and_newer_applied() -> None:
    s = _sync()
    for seq in (3, 4, 5, 6):  # buffered while the snapshot is in flight
        assert s.on_message(E1, seq, seq) is Outcome.BUFFERED
    assert s.load_snapshot(E1, 4, [0]) is Outcome.APPLIED  # snapshot reflects up to seq 4
    assert s.state == [0, 5, 6] and s.last_seq == 6 and not s.syncing
    assert s.on_message(E1, 6, 6) is Outcome.DUPLICATE
    assert s.on_message(E1, 7, 7) is Outcome.APPLIED
    assert s.state == [0, 5, 6, 7]
    assert s.resume_point() == {"epoch": E1, "seq": 7}


def test_gap_while_live_returns_to_syncing_and_resync_recovers() -> None:
    s = _sync()
    s.load_snapshot(E1, 10, [])
    assert s.on_message(E1, 11, 11) is Outcome.APPLIED
    assert s.on_message(E1, 13, 13) is Outcome.GAP  # 12 never arrived
    assert s.syncing and s.gaps == 1 and s.resume_point() is None
    assert s.on_message(E1, 14, 14) is Outcome.BUFFERED  # not applied on top of a hole
    assert s.state == [11]
    assert s.load_snapshot(E1, 13, [11, 12, 13]) is Outcome.APPLIED  # fresh snapshot covers it
    assert s.state == [11, 12, 13, 14] and s.last_seq == 14


def test_snapshot_older_than_the_buffer_needs_another_fetch() -> None:
    s = _sync()
    s.on_message(E1, 9, 9)
    assert s.load_snapshot(E1, 5, [0]) is Outcome.GAP  # 6..8 are in neither
    assert s.syncing


def test_resync_frame_discards_the_buffer() -> None:
    s = _sync()
    s.load_snapshot(E1, 1, [])
    s.on_resync()
    assert s.syncing and s.on_message(E1, 5, 5) is Outcome.BUFFERED


def test_a_new_epoch_is_never_mistaken_for_duplicates() -> None:
    """After a Redis reset the seqs restart at 1. Comparing seqs across epochs would drop every
    new message as 'seq <= last'."""
    s = _sync()
    s.load_snapshot(E1, 50, [50])
    assert s.on_message(E1, 51, 51) is Outcome.APPLIED
    assert s.on_message(E2, 1, 100) is Outcome.EPOCH_CHANGED  # NOT a duplicate of anything
    assert s.syncing and s.epoch_changes == 1
    assert s.on_message(E2, 2, 200) is Outcome.BUFFERED
    # a snapshot fetched from before the reset is detected as stale ...
    assert s.load_snapshot(E1, 60, [60]) is Outcome.EPOCH_CHANGED
    assert s.syncing
    # ... and the post-reset one is applied, with its deltas
    assert s.load_snapshot(E2, 1, [100]) is Outcome.APPLIED
    assert s.state == [100, 200] and s.epoch == E2 and s.last_seq == 2


# ------------------------------------------------------------------------------------------ resume
@pytest.fixture
async def live(make_settings: SettingsFactory) -> AsyncIterator[Live]:
    async with Harness(make_settings()) as h:
        await seed_market(h)
        async with Live(h) as lv:
            yield lv


def rp(epoch: str, seq: int) -> dict[str, Any]:
    return {"epoch": epoch, "seq": seq}


async def test_resume_replays_exactly_the_missed_messages_in_order(live: Live) -> None:
    async with live.connect() as c:
        sub = await c.subscribe(PRICE)
        epoch = sub["channels"][0]["epoch"]
        for i in range(3):
            await pub(live.h, PRICE, i=i)
        seen = [m["seq"] for m in await c.recv_data(3)]
    assert seen == [1, 2, 3]
    for i in range(3, 8):  # missed while disconnected
        await pub(live.h, PRICE, i=i)
    async with live.connect() as c:
        await c.send(op="subscribe", channels=[PRICE], resume={PRICE: rp(epoch, 3)}, id="r")
        replay = await c.recv_data(6)  # 5 replayed messages, then the `subscribed` frame
        assert [m["seq"] for m in replay[:5]] == [4, 5, 6, 7, 8]
        assert [m["data"]["i"] for m in replay[:5]] == [3, 4, 5, 6, 7]
        assert {m["epoch"] for m in replay[:5]} == {epoch}
        sub = replay[5]
        assert sub["type"] == "subscribed" and sub["channels"][0] == {
            "channel": PRICE,
            "epoch": epoch,
            "seq": 8,
            "replayed": 5,
        }
        await pub(live.h, PRICE, i=8)
        nxt = await c.recv_type("price")
        assert nxt["seq"] == 9  # live continues with no duplicate and no gap


async def test_resume_at_head_replays_nothing(live: Live) -> None:
    await pub(live.h, PRICE, i=0)
    epoch = (await publisher.publish(live.h.app.state.redis, PRICE, "price", {"i": 1})).epoch
    async with live.connect() as c:
        sub = await c.subscribe(PRICE, resume={PRICE: rp(epoch, 2)})
        assert sub["channels"][0]["replayed"] == 0


async def test_resume_with_a_stale_epoch_sends_resync_epoch_changed(live: Live) -> None:
    await pub(live.h, PRICE, i=0)
    async with live.connect() as c:
        await c.send(op="subscribe", channels=[PRICE], resume={PRICE: rp("some-old-epoch", 1)})
        rs = await c.recv_type("resync")
        assert (rs["channel"], rs["reason"], rs["seq"]) == (PRICE, "epoch_changed", 1)
        sub = await c.recv_type("subscribed")
        assert rs["epoch"] == sub["channels"][0]["epoch"] != "some-old-epoch"


async def test_resume_without_an_epoch_is_a_protocol_error(live: Live) -> None:
    async with live.connect() as c:
        await c.ws.send(json.dumps({"op": "subscribe", "channels": [PRICE], "resume": {PRICE: 3}}))
        assert await c.expect_close() == 4400


async def test_resume_outside_the_window_sends_resync(make_settings: SettingsFactory) -> None:
    async with Harness(make_settings(ws_retain_price_len=10)) as h:  # keeps the last 10 only
        await seed_market(h)
        for i in range(60):
            await pub(h, PRICE, i=i)
        epoch = (await publisher.publish(h.app.state.redis, PRICE, "price", {"i": 60})).epoch
        async with Live(h) as lv, lv.connect() as c:
            await c.send(op="subscribe", channels=[PRICE], resume={PRICE: rp(epoch, 2)})
            rs = await c.recv_type("resync")
            assert (rs["channel"], rs["reason"], rs["seq"]) == (PRICE, "too_old", 61)
            assert (await c.recv_type("subscribed"))["channels"][0]["seq"] == 61
            await pub(h, PRICE, i=61)
            assert (await c.recv_type("price"))["seq"] == 62


async def test_resume_ahead_of_head_sends_resync(live: Live) -> None:
    epoch = (await publisher.publish(live.h.app.state.redis, PRICE, "price", {"i": 0})).epoch
    async with live.connect() as c:
        await c.send(op="subscribe", channels=[PRICE], resume={PRICE: rp(epoch, 999)})
        rs = await c.recv_type("resync")
        assert (rs["reason"], rs["seq"]) == ("ahead_of_head", 1)


async def test_resume_with_too_many_missed_messages_sends_resync(
    make_settings: SettingsFactory,
) -> None:
    async with Harness(make_settings(ws_resume_max_messages=3)) as h:
        await seed_market(h)
        for i in range(10):
            await pub(h, PRICE, i=i)
        epoch = (await publisher.publish(h.app.state.redis, PRICE, "price", {"i": 10})).epoch
        async with Live(h) as lv, lv.connect() as c:
            await c.send(op="subscribe", channels=[PRICE], resume={PRICE: rp(epoch, 2)})
            assert (await c.recv_type("resync"))["reason"] == "too_many"


async def test_resume_while_publishing_has_no_gap_and_no_duplicate(live: Live) -> None:
    """Live delivery and replay overlap: every seq from last+1 arrives exactly once, in order."""
    for i in range(20):
        await pub(live.h, PRICE, i=i)
    epoch = (await publisher.publish(live.h.app.state.redis, PRICE, "price", {"i": 20})).epoch
    stop = asyncio.Event()

    async def producer() -> int:
        n = 21
        while not stop.is_set():
            await pub(live.h, PRICE, i=n)
            n += 1
            await asyncio.sleep(0)
        return n

    task = asyncio.create_task(producer())
    async with live.connect() as c:
        await c.send(op="subscribe", channels=[PRICE], resume={PRICE: rp(epoch, 5)}, id="r")
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


# ------------------------------------------------------- Redis reset mid-session: the epoch changes
async def test_redis_flush_mid_session_is_detected_and_the_client_resyncs(
    make_settings: SettingsFactory,
) -> None:
    """A live socket and a client that follows the protocol. Redis is flushed (channel state lost,
    seqs restart at 1). Without an epoch the new messages (seq 1, 2 ...) look like duplicates of
    what the client already applied and are silently ignored. With it the gateway sends `resync`,
    the client detects the epoch change and ends up with the post-reset state."""
    async with Harness(make_settings()) as h:
        await seed_market(h)
        redis, sm = h.app.state.redis, h.app.state.sessionmaker

        def apply(_: dict[str, Any], d: dict[str, Any]) -> dict[str, Any]:
            return {"v": d["v"]}

        sync: ChannelSync[dict[str, Any], dict[str, Any]] = ChannelSync(apply)
        async with Live(h) as lv, httpx.AsyncClient(base_url=lv.http) as http:

            async def fetch() -> Outcome:
                r = await http.get(f"/api/v1/markets/{MID}/book")
                body = r.json()
                assert r.status_code == 200 and body["epoch"]
                return sync.load_snapshot(body["epoch"], body["seq"], {"v": body["ts"]})

            async with lv.connect() as ws:
                sub = await ws.subscribe(BOOK)
                epoch_before = sub["channels"][0]["epoch"]
                assert await fetch() is Outcome.APPLIED
                assert sync.epoch == epoch_before

                for v in (1, 2, 3):
                    await pub(h, BOOK, "orderbook", v=v)
                for m in await ws.recv_data(3):
                    assert sync.on_message(m["epoch"], m["seq"], m["data"]) is Outcome.APPLIED
                assert sync.last_seq == 3 and sync.state == {"v": 3}

                await redis.flushdb()  # <- Redis reset
                for v in (10, 20):
                    await pub(h, BOOK, "orderbook", v=v)

                outcomes: list[Outcome] = []
                frames: list[dict[str, Any]] = []
                while len(frames) < 3:  # resync frame, then the two post-reset messages
                    m = await ws.recv(5)
                    if m["type"] == "ping":
                        continue
                    frames.append(m)
                    if m["type"] == "resync":
                        assert m["reason"] == "epoch_changed" and m["epoch"] != epoch_before
                        sync.on_resync()
                    elif m["type"] == "orderbook":
                        assert m["epoch"] != epoch_before and m["seq"] in (1, 2)
                        outcomes.append(sync.on_message(m["epoch"], m["seq"], m["data"]))
                assert [f["type"] for f in frames] == ["resync", "orderbook", "orderbook"]
                assert outcomes == [Outcome.BUFFERED, Outcome.BUFFERED]
                assert sync.syncing  # the client did NOT carry on as if nothing happened

                # re-snapshot over REST: the book is rebuilt after the reset and published afresh
                await publish_book(redis, sm, MID)
                assert await fetch() is Outcome.APPLIED
                assert sync.epoch != epoch_before and not sync.syncing
                # live again in the new epoch
                await pub(h, BOOK, "orderbook", v=30)
                while True:
                    m = await ws.recv_type("orderbook")
                    if m["seq"] > (sync.last_seq or 0):  # skip what the snapshot already covers
                        break
                assert sync.on_message(m["epoch"], m["seq"], m["data"]) is Outcome.APPLIED
                assert sync.state == {"v": 30}


async def test_a_flush_while_disconnected_makes_resume_answer_resync(live: Live) -> None:
    async with live.connect() as c:
        sub = await c.subscribe(PRICE)
        epoch = sub["channels"][0]["epoch"]
        await pub(live.h, PRICE, i=0)
        await c.recv_type("price")
    await live.h.app.state.redis.flushdb()
    for i in range(
        3
    ):  # the new epoch has seqs 1..3 -- a naive seq check would replay "4.." as nothing
        await pub(live.h, PRICE, i=i)
    async with live.connect() as c:
        await c.send(op="subscribe", channels=[PRICE], resume={PRICE: rp(epoch, 1)})
        rs = await c.recv_type("resync")
        assert rs["reason"] == "epoch_changed" and rs["epoch"] != epoch


# ------------------------------------------------------------------- REST snapshots carry the epoch
async def test_rest_snapshots_carry_epoch_and_seq(live: Live) -> None:
    async with httpx.AsyncClient(base_url=live.http) as http:
        b0 = (await http.get(f"/api/v1/markets/{MID}/book")).json()
        assert b0["seq"] == 0 and len(b0["epoch"]) == 16
        await publisher.publish(live.h.app.state.redis, BOOK, "orderbook", {"x": 1})
        b1 = (await http.get(f"/api/v1/markets/{MID}/book")).json()
        assert (b1["seq"], b1["epoch"]) == (1, b0["epoch"])  # the epoch the subscriber saw
        t = await http.get(f"/api/v1/markets/{MID}/trades")
        assert t.headers["x-channel-epoch"] and t.headers["x-channel-seq"] == "0"
    async with live.connect() as c:
        sub = await c.subscribe(BOOK)
        assert sub["channels"][0]["epoch"] == b0["epoch"]


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
                    body["epoch"], int(body["seq"]), {"bids": body["bids"], "asks": body["asks"]}
                )

            async with lv.connect() as ws:
                await ws.subscribe(BOOK)
                assert await fetch() is Outcome.APPLIED

                await seller.place_ok("sell", 1_000, 140_000)
                m1 = await ws.recv_type("orderbook")
                assert sync.on_message(m1["epoch"], m1["seq"], m1["data"]) is Outcome.APPLIED

                await redis.incr(channels.seq_key(BOOK))  # a message lost in transit
                await seller.place_ok("sell", 2_000, 145_000)
                m3 = await ws.recv_type("orderbook")
                assert m3["seq"] == m1["seq"] + 2
                assert sync.on_message(m3["epoch"], m3["seq"], m3["data"]) is Outcome.GAP

                assert await fetch() is Outcome.APPLIED  # re-snapshot over REST
                server = (await http.get(f"/api/v1/markets/{MID}/book")).json()
                assert sync.state == {"bids": server["bids"], "asks": server["asks"]}
                assert [lv_["price"] for lv_ in sync.state["asks"]] == ["140000", "145000"]
                assert sync.last_seq == server["seq"]

                await seller.place_ok("sell", 500, 150_000)
                m4 = await ws.recv_type("orderbook")
                assert sync.on_message(m4["epoch"], m4["seq"], m4["data"]) is Outcome.APPLIED
                assert len(sync.state["asks"]) == 3
