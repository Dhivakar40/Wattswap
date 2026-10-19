"""One WebSocket: bounded send queue, per-channel sequencing, replay, rate limit.

Ordering rule: for each subscribed channel the connection forwards a message only if its seq is
greater than the last one it sent, so duplicates (replay overlapping live delivery) are dropped and
order is preserved. While a channel is being synced (resume/subscribe) live messages are buffered
and flushed right after the replay.
"""

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import redis.asyncio as aioredis
from fastapi import WebSocket
from pydantic import BaseModel

from app.auth.tokens import TokenClaims
from app.core.config import Settings
from app.core.metrics import WS_CLOSES, WS_MESSAGES_OUT, WS_QUEUE_DEPTH, WS_RESUME
from app.realtime import protocol, streams
from app.realtime.protocol import Close

if TYPE_CHECKING:
    from app.realtime.hub import Hub

log = logging.getLogger("wattswap.ws")

BUFFER_LIMIT = 5000


@dataclass
class ChannelState:
    last_sent: int = 0
    buffer: list[tuple[int, str]] | None = field(default_factory=list)  # None = live


class Connection:
    def __init__(
        self,
        ws: WebSocket,
        settings: Settings,
        redis: aioredis.Redis,
        hub: "Hub",
        conn_id: str,
        ip: str,
        claims: TokenClaims | None,
    ) -> None:
        self.ws = ws
        self.settings = settings
        self.redis = redis
        self.hub = hub
        self.id = conn_id
        self.ip = ip
        self.claims = claims
        self.queue: asyncio.Queue[str] = asyncio.Queue(maxsize=settings.ws_send_queue)
        self.subs: dict[str, ChannelState] = {}
        self.closed = asyncio.Event()
        self.close_code = Close.NORMAL
        self.close_reason = ""
        self.last_rx = time.monotonic()
        self.opened_at = time.monotonic()
        self.queue_high_water = 0
        self.sent = 0
        self.sending_since: float | None = None  # set while the writer is inside send_text
        self._tokens = float(settings.ws_rate_burst)
        self._refill_at = time.monotonic()
        self._strikes = 0
        self._catch_ups: set[asyncio.Task[None]] = set()
        self._out: dict[str, int] = {}  # frames queued by kind; flushed to Prometheus on close

    # ------------------------------------------------------------------ closing
    def close(self, code: int, reason: str) -> None:
        """Mark the socket for closing (idempotent, synchronous, callable from anywhere)."""
        if self.closed.is_set():
            return
        self.close_code = code
        self.close_reason = reason
        self.closed.set()
        WS_CLOSES.labels(str(code)).inc()
        for kind, n in self._out.items():  # per-frame metric calls are too costly on the hot path
            WS_MESSAGES_OUT.labels(kind).inc(n)
        log.info("ws closing", extra={"conn": self.id, "code": code, "reason": reason})

    # ------------------------------------------------------------------ outbound
    def send_stalled(self) -> bool:
        """The writer has been inside one send longer than `ws_send_timeout_seconds`."""
        since = self.sending_since
        return (
            since is not None and time.monotonic() - since > self.settings.ws_send_timeout_seconds
        )

    def enqueue(self, raw: str, kind: str = "data") -> None:
        if self.closed.is_set():
            return
        try:
            self.queue.put_nowait(raw)
        except asyncio.QueueFull:
            self.close(Close.SLOW_CONSUMER, "slow_consumer")
            return
        depth = self.queue.qsize()
        if depth > self.queue_high_water:
            self.queue_high_water = depth
        if depth > 1 or self.sending_since is not None:
            WS_QUEUE_DEPTH.observe(depth)  # only interesting when there is a backlog
        self._out[kind] = self._out.get(kind, 0) + 1
        if self.send_stalled():
            self.close(Close.SLOW_CONSUMER, "slow_consumer")

    def send(self, frame: BaseModel) -> None:
        self.enqueue(protocol.dump(frame), type(frame).__name__.lower())

    def deliver(self, channel: str, seq: int, raw: str) -> None:
        """Called by the hub for every message on a channel this socket joined."""
        state = self.subs.get(channel)
        if state is None:
            return
        if state.buffer is not None:
            if len(state.buffer) >= BUFFER_LIMIT:
                self.close(Close.SLOW_CONSUMER, "sync_buffer_overflow")
                return
            state.buffer.append((seq, raw))
            return
        if seq > state.last_sent:
            state.last_sent = seq
            self.enqueue(raw)

    # ------------------------------------------------------------------ join / replay
    async def join(self, channel: str) -> None:
        """Register interest. Live messages are buffered until `sync` + `release`."""
        self.subs[channel] = ChannelState()
        try:
            await self.hub.attach(self, channel)
        except BaseException:
            self.subs.pop(channel, None)
            await self.hub.detach(self, channel)
            raise

    async def sync(self, channel: str, resume_from: int | None) -> tuple[int, int]:
        """Replay what the client missed (or just learn the head). Returns (head, replayed).

        A `resync` frame is queued when the log cannot cover the gap."""
        state = self.subs[channel]
        state.buffer = [] if state.buffer is None else state.buffer
        if resume_from is None:
            head = await streams.head_seq(self.redis, channel)
            state.last_sent = head
            return head, 0
        replay = await streams.read_since(
            self.redis, channel, resume_from, max_messages=self.settings.ws_resume_max_messages
        )
        if replay.resync is not None:
            WS_RESUME.labels("resync").inc()
            self.send(protocol.Resync(channel=channel, reason=replay.resync.value, seq=replay.head))
        else:
            WS_RESUME.labels("replayed" if replay.messages else "current").inc()
            for _, raw in replay.messages:
                self.enqueue(raw)
        state.last_sent = replay.head
        return replay.head, len(replay.messages)

    def release(self, channel: str) -> None:
        """Go live: flush buffered live messages newer than what was replayed, in seq order."""
        state = self.subs.get(channel)
        if state is None or state.buffer is None:
            return
        pending, state.buffer = sorted(state.buffer), None
        for seq, raw in pending:
            if seq > state.last_sent:
                state.last_sent = seq
                self.enqueue(raw)

    def schedule_catch_up(self, channel: str) -> None:
        """After the hub lost its Redis connection: replay from our own last-sent seq."""
        state = self.subs.get(channel)
        if state is None:
            return

        async def run() -> None:
            current = self.subs.get(channel)
            if current is None:
                return
            current.buffer = []
            try:
                await self.sync(channel, current.last_sent)
            finally:
                self.release(channel)

        task = asyncio.create_task(run(), name=f"ws-catchup-{self.id}")
        self._catch_ups.add(task)
        task.add_done_callback(self._catch_ups.discard)

    async def leave(self, channel: str) -> None:
        if self.subs.pop(channel, None) is not None:
            await self.hub.detach(self, channel)

    async def leave_all(self) -> None:
        for task in tuple(self._catch_ups):
            task.cancel()
        for channel in tuple(self.subs):
            await self.leave(channel)

    # ------------------------------------------------------------------ inbound rate limit
    def allow_message(self) -> bool:
        """Token bucket: `ws_rate_per_window` per window sustained, `ws_rate_burst` burst."""
        now = time.monotonic()
        rate = self.settings.ws_rate_per_window / self.settings.ws_rate_window_seconds
        self._tokens = min(
            float(self.settings.ws_rate_burst), self._tokens + (now - self._refill_at) * rate
        )
        self._refill_at = now
        if self._tokens >= 1.0:
            self._tokens -= 1.0
            return True
        self._strikes += 1
        return False

    @property
    def rate_strikes(self) -> int:
        return self._strikes
