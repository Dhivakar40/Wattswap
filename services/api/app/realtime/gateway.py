"""The /ws endpoint: handshake policy, auth, limits, heartbeat and the request handlers."""

import asyncio
import contextlib
import logging
import secrets
import time

import redis.asyncio as aioredis
from fastapi import WebSocket
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.auth.tokens import TokenClaims, decode_token, is_revoked
from app.core.config import Settings
from app.core.metrics import WS_CLOSES, WS_CONNECTIONS, WS_HANDSHAKES, WS_SUBSCRIPTIONS
from app.models import Market, Meter, User
from app.realtime import channels, protocol
from app.realtime.channels import Channel, Kind
from app.realtime.connection import Connection
from app.realtime.hub import Hub
from app.realtime.protocol import Close

log = logging.getLogger("wattswap.ws")

MARKET_CACHE_SECONDS = 30.0
CLOSE_SEND_TIMEOUT = 3.0

# KEYS: zsets of live connection ids (one per limit). ARGV: now, stale_after, id, max_1..max_n
_ADMIT = """
local now = tonumber(ARGV[1])
local stale = tonumber(ARGV[2])
for i, key in ipairs(KEYS) do
  redis.call('ZREMRANGEBYSCORE', key, '-inf', now - stale)
  if redis.call('ZCARD', key) >= tonumber(ARGV[3 + i]) then return 0 end
end
for _, key in ipairs(KEYS) do
  redis.call('ZADD', key, now, ARGV[3])
  redis.call('EXPIRE', key, math.ceil(stale * 2))
end
return 1
"""


def origin_allowed(origin: str | None, allowed: frozenset[str], *, allow_missing: bool) -> bool:
    """Exact-match allow-list. A missing Origin (non-browser client) needs `allow_missing`."""
    if origin is None:
        return allow_missing
    return origin.rstrip("/").lower() in allowed


class Gateway:
    def __init__(
        self,
        settings: Settings,
        redis: aioredis.Redis,
        sessionmaker: async_sessionmaker[AsyncSession],
    ) -> None:
        self.settings = settings
        self.redis = redis
        self.sessionmaker = sessionmaker
        self.hub = Hub(redis)
        self._markets: frozenset[str] = frozenset()
        self._markets_at = 0.0

    async def start(self) -> None:
        await self.hub.start()

    async def stop(self) -> None:
        for conn in tuple(self.hub.connections):
            conn.close(Close.GOING_AWAY, "server_shutdown")
        await asyncio.sleep(0.2)  # let each serve() send its close frame
        await self.hub.stop()

    # ------------------------------------------------------------------ handshake
    async def _claims(self, ws: WebSocket) -> TokenClaims | None:
        token = ws.cookies.get(self.settings.session_cookie_name)
        if not token:
            return None
        claims = decode_token(self.settings, token)
        if claims is None or await is_revoked(self.redis, claims.jti):
            return None
        return claims

    async def _admit(self, conn_id: str, ip: str, address: str | None) -> bool:
        keys = [f"{{ws:conn}}:ip:{ip}"]
        limits = [self.settings.ws_max_conn_per_ip]
        if address is not None:
            keys.append(f"{{ws:conn}}:addr:{address}")
            limits.append(self.settings.ws_max_conn_per_address)
        stale = self.settings.ws_ping_seconds * 3
        result = await self.redis.eval(
            _ADMIT, len(keys), *keys, time.time(), stale, conn_id, *limits
        )
        return bool(result)

    async def _release_slot(self, conn: Connection) -> None:
        keys = [f"{{ws:conn}}:ip:{conn.ip}"]
        if conn.claims is not None:
            keys.append(f"{{ws:conn}}:addr:{conn.claims.address}")
        with contextlib.suppress(Exception):
            async with self.redis.pipeline(transaction=False) as pipe:
                for key in keys:
                    pipe.zrem(key, conn.id)
                await pipe.execute()

    async def _touch_slot(self, conn: Connection) -> None:
        keys = [f"{{ws:conn}}:ip:{conn.ip}"]
        if conn.claims is not None:
            keys.append(f"{{ws:conn}}:addr:{conn.claims.address}")
        async with self.redis.pipeline(transaction=False) as pipe:
            for key in keys:
                pipe.zadd(key, {conn.id: time.time()}, xx=True)
            await pipe.execute()

    async def serve(self, ws: WebSocket) -> None:
        origin = ws.headers.get("origin")
        if not origin_allowed(
            origin, self.settings.ws_origins, allow_missing=self.settings.ws_allow_missing_origin
        ):
            WS_HANDSHAKES.labels("bad_origin").inc()
            log.warning("ws handshake refused", extra={"origin": origin})
            await ws.close(code=Close.POLICY)  # before accept => HTTP 403, no WebSocket at all
            return
        claims = await self._claims(ws)
        ip = ws.client.host if ws.client else "unknown"
        conn = Connection(ws, self.settings, self.redis, self.hub, secrets.token_hex(8), ip, claims)
        await ws.accept()
        if not await self._admit(conn.id, ip, claims.address if claims else None):
            WS_HANDSHAKES.labels("too_many_connections").inc()
            WS_CLOSES.labels(str(Close.RATE_LIMITED)).inc()
            await ws.close(code=Close.RATE_LIMITED, reason="too_many_connections")
            return
        WS_HANDSHAKES.labels("ok").inc()
        WS_CONNECTIONS.inc()
        self.hub.register(conn)
        loop = asyncio.get_running_loop()
        expiry: asyncio.TimerHandle | None = None
        if claims is not None:
            expiry = loop.call_later(
                max(claims.expires_at - time.time(), 0),
                conn.close,
                Close.SESSION_ENDED,
                "session_expired",
            )
        tasks = [
            asyncio.create_task(self._writer(conn), name=f"ws-writer-{conn.id}"),
            asyncio.create_task(self._reader(conn), name=f"ws-reader-{conn.id}"),
            asyncio.create_task(self._heartbeat(conn), name=f"ws-heartbeat-{conn.id}"),
        ]
        try:
            await conn.closed.wait()
        finally:
            if expiry is not None:
                expiry.cancel()
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            with contextlib.suppress(Exception):
                await asyncio.wait_for(
                    ws.close(code=conn.close_code, reason=conn.close_reason), CLOSE_SEND_TIMEOUT
                )
            await conn.leave_all()
            self.hub.unregister(conn)
            await self._release_slot(conn)
            WS_CONNECTIONS.dec()
            WS_SUBSCRIPTIONS.set(sum(len(c.subs) for c in self.hub.connections))

    # ------------------------------------------------------------------ writer / heartbeat
    async def _writer(self, conn: Connection) -> None:
        # No per-send timer (that costs a task/timeout per frame per socket). A stalled send is
        # detected instead by `Connection.enqueue`/the heartbeat via `sending_since`.
        while True:
            raw = await conn.queue.get()
            conn.sending_since = time.monotonic()
            try:
                await conn.ws.send_text(raw)
            except Exception:
                # The peer is gone; the close code would be unobservable anyway.
                conn.close(Close.NORMAL, "peer_gone")
                return
            conn.sending_since = None
            conn.sent += 1

    async def _heartbeat(self, conn: Connection) -> None:
        interval = self.settings.ws_ping_seconds
        last_denylist_check = time.monotonic()
        while True:
            await asyncio.sleep(interval)
            now = time.monotonic()
            if now - conn.last_rx > interval * 2:
                conn.close(Close.HEARTBEAT_TIMEOUT, "heartbeat_timeout")
                return
            if conn.send_stalled():
                conn.close(Close.SLOW_CONSUMER, "slow_consumer")
                return
            conn.send(protocol.PingFrame(ts=int(time.time())))
            with contextlib.suppress(Exception):
                await self._touch_slot(conn)
            if (
                conn.claims is not None
                and now - last_denylist_check >= self.settings.ws_denylist_poll_seconds
            ):
                # Backstop for a lost `ws:control` message (pub/sub is lossy).
                last_denylist_check = now
                if await is_revoked(self.redis, conn.claims.jti):
                    conn.close(Close.SESSION_ENDED, "session_revoked")
                    return

    # ------------------------------------------------------------------ reader
    async def _reader(self, conn: Connection) -> None:
        limit = self.settings.ws_max_message_bytes
        while True:
            msg = await conn.ws.receive()
            if msg["type"] == "websocket.disconnect":
                conn.close(Close.NORMAL, "client_left")
                return
            conn.last_rx = time.monotonic()
            text = msg.get("text")
            if text is None:
                conn.close(Close.BAD_MESSAGE, "binary_frames_not_supported")
                return
            if len(text.encode()) > limit:
                conn.close(Close.TOO_LARGE, "message_too_large")
                return
            if not conn.allow_message():
                if conn.rate_strikes >= self.settings.ws_rate_strikes:
                    conn.close(Close.RATE_LIMITED, "rate_limited")
                    return
                conn.send(protocol.Error(code="rate_limited", message="slow down"))
                continue
            try:
                request = protocol.INBOUND.validate_json(text)
            except ValidationError:
                conn.close(Close.BAD_MESSAGE, "bad_message")
                return
            if isinstance(request, protocol.Subscribe):
                await self._subscribe(conn, request)
            elif isinstance(request, protocol.Unsubscribe):
                await self._unsubscribe(conn, request)
            elif isinstance(request, protocol.Ping):
                conn.send(protocol.PongFrame(ref=request.id))
            # Pong: liveness was already recorded above.

    # ------------------------------------------------------------------ subscribe
    async def _known_markets(self) -> frozenset[str]:
        if time.monotonic() - self._markets_at > MARKET_CACHE_SECONDS:
            async with self.sessionmaker() as s:
                rows = await s.scalars(select(Market.market_id))
                self._markets = frozenset(rows)
            self._markets_at = time.monotonic()
        return self._markets

    async def _owns_meter(self, meter_id: str, address: str) -> bool:
        async with self.sessionmaker() as s:
            owner = await s.scalar(
                select(User.address)
                .join(Meter, Meter.owner_id == User.id)
                .where(Meter.meter_id == meter_id)
            )
        return owner is not None and owner.lower() == address

    async def _authorize(self, conn: Connection, ch: Channel) -> protocol.Error | None:
        """None if allowed, else the error frame to send (without ref/channel)."""
        if ch.kind == Kind.LOADTEST:
            if not self.settings.ws_loadtest_enabled:
                return protocol.Error(code="bad_channel", message="channel not available")
            return None
        if ch.kind in (Kind.PRICE, Kind.FORECAST, Kind.ORDERBOOK, Kind.TRADES):
            if ch.arg not in await self._known_markets():
                return protocol.Error(code="unknown_market", message="no such market")
            return None
        if conn.claims is None:
            return protocol.Error(code="unauthorized", message="sign in to join this channel")
        if ch.kind == Kind.USER:
            if ch.arg != conn.claims.address:
                return protocol.Error(code="forbidden", message="not your channel")
            return None
        if not await self._owns_meter(ch.arg, conn.claims.address):  # Kind.METER
            return protocol.Error(code="forbidden", message="not your meter")
        return None

    async def _subscribe(self, conn: Connection, req: protocol.Subscribe) -> None:
        resume = {k.lower(): (v.epoch, v.seq) for k, v in (req.resume or {}).items()}
        done: list[protocol.SubscribedChannel] = []
        joined: list[str] = []
        for name in dict.fromkeys(req.channels):  # de-duplicate, keep order
            ch = channels.parse(name)
            if ch is None:
                conn.send(
                    protocol.Error(
                        code="bad_channel",
                        message="malformed channel",
                        ref=req.id,
                        channel=name[:100],
                    )
                )
                continue
            if ch.name in conn.subs:  # idempotent
                st = conn.subs[ch.name]
                done.append(
                    protocol.SubscribedChannel(channel=ch.name, epoch=st.epoch, seq=st.last_sent)
                )
                continue
            if len(conn.subs) >= self.settings.ws_max_subscriptions:
                conn.send(
                    protocol.Error(
                        code="too_many_subscriptions",
                        message=f"at most {self.settings.ws_max_subscriptions} channels per connection",
                        ref=req.id,
                        channel=ch.name,
                    )
                )
                continue
            denied = await self._authorize(conn, ch)
            if denied is not None:
                denied.ref, denied.channel = req.id, ch.name
                conn.send(denied)
                continue
            try:
                await conn.join(ch.name)
                head, replayed = await conn.sync(ch.name, resume.get(ch.name))
            except Exception:
                log.exception("subscribe failed", extra={"channel": ch.name})
                await conn.leave(ch.name)
                conn.send(
                    protocol.Error(
                        code="bad_channel",
                        message="could not subscribe",
                        ref=req.id,
                        channel=ch.name,
                    )
                )
                continue
            done.append(
                protocol.SubscribedChannel(
                    channel=ch.name, epoch=head.epoch, seq=head.seq, replayed=replayed
                )
            )
            joined.append(ch.name)
        if done:
            conn.send(protocol.Subscribed(ref=req.id, channels=done))
        for name in joined:  # live messages go out only after `subscribed`
            conn.release(name)
        WS_SUBSCRIPTIONS.set(sum(len(c.subs) for c in self.hub.connections))

    async def _unsubscribe(self, conn: Connection, req: protocol.Unsubscribe) -> None:
        gone: list[str] = []
        for name in dict.fromkeys(req.channels):
            ch = channels.parse(name)
            if ch is not None and ch.name in conn.subs:
                await conn.leave(ch.name)
                gone.append(ch.name)
        conn.send(protocol.Unsubscribed(ref=req.id, channels=gone))
        WS_SUBSCRIPTIONS.set(sum(len(c.subs) for c in self.hub.connections))
