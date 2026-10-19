"""The atomic publisher: ordering under concurrency, retention, replay and the key layout."""

import asyncio
import json
from collections.abc import AsyncIterator

import pytest
import redis.asyncio as aioredis

from app.realtime import channels, publisher, streams
from app.realtime.channels import Kind, Retention

MID = "0x" + "ab" * 32
CH = f"price:{MID}"


@pytest.fixture
async def redis(redis_ok: str) -> AsyncIterator[aioredis.Redis]:
    r = aioredis.from_url(redis_ok, decode_responses=True, max_connections=1000)
    await r.flushdb()
    yield r
    await r.flushdb()
    await r.aclose()


async def collect(redis: aioredis.Redis, channel: str, n: int, started: asyncio.Event) -> list[int]:
    ps = redis.pubsub()
    await ps.subscribe(channel)
    started.set()
    seqs: list[int] = []
    deadline = asyncio.get_running_loop().time() + 10
    while len(seqs) < n and asyncio.get_running_loop().time() < deadline:
        # None also covers the (ignored) subscribe confirmation, so keep polling until the deadline
        msg = await ps.get_message(ignore_subscribe_messages=True, timeout=1)
        if msg is not None:
            seqs.append(int(json.loads(msg["data"])["seq"]))
    await ps.aclose()  # type: ignore[no-untyped-call]
    return seqs


async def test_concurrent_publishers_deliver_strictly_increasing_seq_without_gaps(
    redis: aioredis.Redis, redis_ok: str
) -> None:
    """Regression for the Phase 4 publisher (INCR then PUBLISH as two round trips), which let
    concurrent publishers deliver seq 6 before seq 5. Many separate connections publish at once."""
    n_pub, per_pub = 20, 25
    total = n_pub * per_pub
    started = asyncio.Event()
    listener = asyncio.create_task(collect(redis, CH, total, started))
    await started.wait()
    clients = [aioredis.from_url(redis_ok, decode_responses=True) for _ in range(n_pub)]

    async def worker(c: aioredis.Redis) -> None:
        for i in range(per_pub):
            await publisher.publish(c, CH, "price", {"i": i})

    await asyncio.gather(*(worker(c) for c in clients))
    seqs = await asyncio.wait_for(listener, 15)
    for c in clients:
        await c.aclose()
    assert seqs == list(range(1, total + 1))  # strictly increasing, no gaps, no reordering
    assert await streams.head_seq(redis, CH) == total


async def test_old_publisher_pattern_does_reorder(redis: aioredis.Redis) -> None:
    """Documents the bug the script fixes: INCR and PUBLISH as separate steps can interleave."""
    started = asyncio.Event()
    n = 400
    listener = asyncio.create_task(collect(redis, "legacy", n, started))
    await started.wait()

    async def legacy(i: int) -> None:
        seq = int(await redis.incr("legacy:seq"))
        await asyncio.sleep(0 if i % 2 else 0.001)  # the gap between the two steps
        await redis.publish("legacy", json.dumps({"seq": seq}))

    await asyncio.gather(*(legacy(i) for i in range(n)))
    seqs = await listener
    assert seqs != sorted(seqs)


async def test_replay_is_exact_and_contiguous(redis: aioredis.Redis) -> None:
    for i in range(10):
        await publisher.publish(redis, CH, "price", {"i": i})
    r = await streams.read_since(redis, CH, 6, max_messages=100)
    assert r.resync is None and r.head == 10
    assert [s for s, _ in r.messages] == [7, 8, 9, 10]
    env = json.loads(r.messages[0][1])
    assert env == {"type": "price", "channel": CH, "seq": 7, "ts": env["ts"], "data": {"i": 6}}
    assert (await streams.read_since(redis, CH, 10, max_messages=100)).messages == []


async def test_resync_reasons(redis: aioredis.Redis) -> None:
    saved = channels.retention_for(CH)
    channels.configure_retention({Kind.PRICE: Retention(5, 3600)})
    try:
        for i in range(40):
            await publisher.publish(redis, CH, "price", {"i": i})
    finally:
        channels.configure_retention({Kind.PRICE: saved})
    # MAXLEN ~ trims in whole nodes, so the window is "at least 5, at most a node more"
    assert (await streams.read_since(redis, CH, 2, max_messages=100)).resync == "too_old"
    assert (await streams.read_since(redis, CH, 99, max_messages=100)).resync == "ahead_of_head"
    assert (await streams.read_since(redis, CH, 0, max_messages=3)).resync == "too_many"


async def test_age_retention_drops_old_entries(redis: aioredis.Redis) -> None:
    saved = channels.retention_for(CH)
    channels.configure_retention({Kind.PRICE: Retention(1000, 1)})
    try:
        await publisher.publish(redis, CH, "price", {"i": 1})
        await asyncio.sleep(1.2)
        await publisher.publish(redis, CH, "price", {"i": 2})
    finally:
        channels.configure_retention({Kind.PRICE: saved})
    assert await redis.xlen(channels.stream_key(CH)) == 1
    assert (await streams.read_since(redis, CH, 0, max_messages=10)).resync == "too_old"
    assert [s for s, _ in (await streams.read_since(redis, CH, 1, max_messages=10)).messages] == [2]


async def test_keys_share_a_hash_tag_and_latest_matches_seq(redis: aioredis.Redis) -> None:
    await publisher.publish(redis, CH, "price", {"x": 1}, keep_latest=True)
    p = await publisher.publish(redis, CH, "price", {"x": 2}, keep_latest=True)
    keys = {channels.seq_key(CH), channels.stream_key(CH), channels.latest_key(CH)}
    assert {k.split("}")[0] for k in keys} == {"{chan:" + CH}
    assert json.loads(str(await redis.get(channels.latest_key(CH))))["seq"] == p.seq == 2


def test_channel_parser_rejects_malformed_names() -> None:
    assert channels.parse(CH) is not None
    assert channels.parse(CH.upper().replace("PRICE", "price")) is not None  # case-normalised
    for bad in [
        "",
        "price",
        "price:",
        "price:0x12",
        f"price:{MID}\n",
        f"price:{MID}:x",
        f"user:{MID}",
        "nope:" + MID,
        "price:{chan:x}",
        "*",
        "price:*",
        "x" * 300,
    ]:
        assert channels.parse(bad) is None, bad
