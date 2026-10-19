"""Reading the retained log: head seq and replay-since for resume."""

from dataclasses import dataclass
from enum import StrEnum
from typing import cast

import redis.asyncio as aioredis

from app.realtime import channels


class ResyncReason(StrEnum):
    TOO_OLD = "too_old"  # missed messages have left the retention window
    AHEAD = "ahead_of_head"  # client seq is beyond the head (Redis was reset)
    TOO_MANY = "too_many"  # more missed messages than one replay allows


@dataclass(frozen=True)
class Replay:
    messages: list[tuple[int, str]]  # (seq, envelope JSON), ascending, contiguous
    head: int
    resync: ResyncReason | None = None


async def head_seq(redis: aioredis.Redis, channel: str) -> int:
    raw = await redis.get(channels.seq_key(channel))
    return int(raw) if raw is not None else 0


async def read_since(
    redis: aioredis.Redis, channel: str, last_seq: int, *, max_messages: int
) -> Replay:
    """Every retained message with seq > `last_seq`, or the reason the client must re-snapshot.

    The replay is returned only when provably contiguous: it must be exactly
    `last_seq + 1 .. head`, with `head` read before the range query. Messages published meanwhile
    arrive over pub/sub and are de-duplicated by seq."""
    head = await head_seq(redis, channel)
    if last_seq > head:
        return Replay([], head, ResyncReason.AHEAD)
    if last_seq == head:
        return Replay([], head)
    if head - last_seq > max_messages:
        return Replay([], head, ResyncReason.TOO_MANY)
    rows = cast(
        "list[tuple[str, dict[str, str]]]",
        await redis.xrange(
            channels.stream_key(channel), min=f"({last_seq}-0", max=f"{head}-0", count=max_messages
        ),
    )
    messages = [(int(entry_id.split("-")[0]), fields["m"]) for entry_id, fields in rows]
    if [s for s, _ in messages] != list(range(last_seq + 1, head + 1)):
        return Replay([], head, ResyncReason.TOO_OLD)
    return Replay(messages, head)
