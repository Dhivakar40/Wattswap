"""Reading the retained log: (epoch, head seq) and replay-since for resume."""

from dataclasses import dataclass
from enum import StrEnum
from typing import cast

import redis.asyncio as aioredis

from app.realtime import channels
from app.realtime.publisher import new_epoch


class ResyncReason(StrEnum):
    EPOCH_CHANGED = "epoch_changed"  # the channel's state was lost and recreated (Redis reset)
    TOO_OLD = "too_old"  # missed messages have left the retention window
    AHEAD = "ahead_of_head"  # client seq is beyond the head of the same epoch
    TOO_MANY = "too_many"  # more missed messages than one replay allows


@dataclass(frozen=True)
class Head:
    epoch: str
    seq: int


@dataclass(frozen=True)
class Replay:
    messages: list[tuple[int, str]]  # (seq, envelope JSON), ascending, contiguous
    head: Head
    resync: ResyncReason | None = None


async def head(redis: aioredis.Redis, channel: str) -> Head:
    """The channel's current epoch and last seq. The epoch is created on first read if the channel
    has none yet, so a client always has one to remember (the publisher keeps an existing one)."""
    ekey = channels.epoch_key(channel)
    await redis.set(ekey, new_epoch(), nx=True)
    epoch, seq = await redis.mget(ekey, channels.seq_key(channel))
    return Head(str(epoch), int(seq) if seq is not None else 0)


async def read_since(
    redis: aioredis.Redis, channel: str, epoch: str, last_seq: int, *, max_messages: int
) -> Replay:
    """Every retained message with seq > `last_seq` of `epoch`, or why the client must re-snapshot.

    The replay is returned only when provably contiguous: it must be exactly
    `last_seq + 1 .. head`, with `head` read before the range query. Messages published meanwhile
    arrive over pub/sub and are de-duplicated by seq."""
    h = await head(redis, channel)
    if epoch != h.epoch:
        return Replay([], h, ResyncReason.EPOCH_CHANGED)
    if last_seq > h.seq:
        return Replay([], h, ResyncReason.AHEAD)
    if last_seq == h.seq:
        return Replay([], h)
    if h.seq - last_seq > max_messages:
        return Replay([], h, ResyncReason.TOO_MANY)
    rows = cast(
        "list[tuple[str, dict[str, str]]]",
        await redis.xrange(
            channels.stream_key(channel),
            min=f"({last_seq}-0",
            max=f"{h.seq}-0",
            count=max_messages,
        ),
    )
    messages = [(int(entry_id.split("-")[0]), fields["m"]) for entry_id, fields in rows]
    if [s for s, _ in messages] != list(range(last_seq + 1, h.seq + 1)):
        return Replay([], h, ResyncReason.TOO_OLD)
    return Replay(messages, h)
