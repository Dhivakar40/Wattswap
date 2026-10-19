"""Per-process fan-out hub: ONE Redis pub/sub connection shared by every local socket.

A Redis channel is subscribed when its first local socket joins and unsubscribed when the last
leaves, so a replica only receives what its own clients want. Publishers run in any process; Redis
is the bus, which is what makes several API replicas behave as one gateway.

If the pub/sub connection drops, the hub re-subscribes and asks every socket to catch up from its
own last-sent seq (replay from the retained log, or a `resync` frame when that is impossible).
"""

import asyncio
import contextlib
import json
import logging
from typing import TYPE_CHECKING

import redis.asyncio as aioredis
from redis.asyncio.client import PubSub
from redis.exceptions import RedisError

if TYPE_CHECKING:
    from app.realtime.connection import Connection

log = logging.getLogger("wattswap.ws.hub")

CONTROL_CHANNEL = "ws:control"
SUBSCRIBE_CONFIRM_TIMEOUT = 5.0


async def announce_session_ended(redis: aioredis.Redis, jti: str) -> None:
    """Tell every replica to close the sockets opened with this session (logout)."""
    await redis.publish(CONTROL_CHANNEL, json.dumps({"op": "revoke", "jti": jti}))


class Hub:
    def __init__(self, redis: aioredis.Redis) -> None:
        self._redis = redis
        self._ps: PubSub | None = None
        self._subs: dict[str, set[Connection]] = {}
        self._confirm: dict[str, asyncio.Future[None]] = {}
        self._by_jti: dict[str, set[Connection]] = {}
        self._conns: set[Connection] = set()
        self._task: asyncio.Task[None] | None = None
        self._stopping = False
        self.reconnects = 0

    # ------------------------------------------------------------------ lifecycle
    async def start(self) -> None:
        self._ps = self._redis.pubsub()
        await self._ps.subscribe(CONTROL_CHANNEL)
        self._task = asyncio.create_task(self._run(), name="ws-hub")

    async def stop(self) -> None:
        self._stopping = True
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._task
        if self._ps is not None:
            with contextlib.suppress(Exception):
                await self._ps.aclose()  # type: ignore[no-untyped-call]

    @property
    def connections(self) -> set["Connection"]:
        return self._conns

    def channel_count(self) -> int:
        return len(self._subs)

    # ------------------------------------------------------------------ membership
    def register(self, conn: "Connection") -> None:
        self._conns.add(conn)
        if conn.claims is not None:
            self._by_jti.setdefault(conn.claims.jti, set()).add(conn)

    def unregister(self, conn: "Connection") -> None:
        self._conns.discard(conn)
        if conn.claims is not None:
            group = self._by_jti.get(conn.claims.jti)
            if group is not None:
                group.discard(conn)
                if not group:
                    del self._by_jti[conn.claims.jti]

    async def attach(self, conn: "Connection", channel: str) -> None:
        """Join `channel`; returns once Redis confirms the SUBSCRIBE is live."""
        self._subs.setdefault(channel, set()).add(conn)
        fut = self._confirm.get(channel)
        if fut is None:
            assert self._ps is not None
            fut = asyncio.get_running_loop().create_future()
            self._confirm[channel] = fut
            await self._ps.subscribe(channel)
        await asyncio.wait_for(asyncio.shield(fut), SUBSCRIBE_CONFIRM_TIMEOUT)

    async def detach(self, conn: "Connection", channel: str) -> None:
        group = self._subs.get(channel)
        if group is None:
            return
        group.discard(conn)
        if not group:
            del self._subs[channel]
            self._confirm.pop(channel, None)
            if self._ps is not None:
                with contextlib.suppress(RedisError, OSError):
                    await self._ps.unsubscribe(channel)

    # ------------------------------------------------------------------ reader
    async def _run(self) -> None:
        backoff = 0.2
        while not self._stopping:
            try:
                assert self._ps is not None
                msg = await self._ps.get_message(ignore_subscribe_messages=False, timeout=1.0)
                if msg is None:
                    continue
                backoff = 0.2
                self._handle(msg)
            except asyncio.CancelledError:
                raise
            except (RedisError, OSError) as exc:
                log.warning("pub/sub connection lost: %s", exc)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 5.0)
                await self._reconnect()
            except Exception:
                log.exception("hub reader error")

    def _handle(self, msg: dict[str, object]) -> None:
        kind = msg.get("type")
        channel = str(msg.get("channel"))
        if kind == "subscribe":
            fut = self._confirm.get(channel)
            if fut is not None and not fut.done():
                fut.set_result(None)
            return
        if kind != "message":
            return
        raw = str(msg["data"])
        if channel == CONTROL_CHANNEL:
            self._control(raw)
            return
        group = self._subs.get(channel)
        if not group:
            return
        try:
            env = json.loads(raw)  # parsed ONCE per message, not per client
            epoch, seq = str(env["epoch"]), int(env["seq"])
        except (ValueError, KeyError, TypeError):
            log.error("dropping malformed envelope on %s", channel)
            return
        for conn in tuple(group):
            conn.deliver(channel, epoch, seq, raw)  # every client gets the same `raw` str object

    def _control(self, raw: str) -> None:
        try:
            body = json.loads(raw)
        except ValueError:
            return
        if body.get("op") == "revoke":
            from app.realtime.protocol import Close

            for conn in tuple(self._by_jti.get(str(body.get("jti")), ())):
                conn.close(Close.SESSION_ENDED, "session_revoked")

    async def _reconnect(self) -> None:
        self.reconnects += 1
        old = self._ps
        self._ps = self._redis.pubsub()
        if old is not None:
            with contextlib.suppress(Exception):
                await old.aclose()  # type: ignore[no-untyped-call]
        await self._ps.subscribe(CONTROL_CHANNEL, *self._subs.keys())
        # Messages published while we were away are gone from pub/sub but not from the log.
        for channel, group in list(self._subs.items()):
            for conn in tuple(group):
                conn.schedule_catch_up(channel)
