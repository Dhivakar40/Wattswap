"""Helpers for gateway tests: a real uvicorn server around the app and a small WS client."""

import asyncio
import json
import socket
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import uvicorn
from websockets.asyncio.client import ClientConnection, connect
from websockets.typing import Origin

from app.pricing.repo import DEFAULT_MARKET_ID, ensure_default_market
from app.realtime import publisher
from tests.conftest import Harness

MID = DEFAULT_MARKET_ID
WEB_ORIGIN = "http://localhost:3000"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


async def seed_market(h: Harness) -> None:
    async with h.app.state.sessionmaker() as s:
        await ensure_default_market(s)


class Live:
    """The app (lifespan already running inside `Harness`) served on a real TCP port."""

    def __init__(self, h: Harness) -> None:
        self.h = h
        self.port = free_port()
        self.server = uvicorn.Server(
            uvicorn.Config(
                h.app, host="127.0.0.1", port=self.port, lifespan="off", log_level="warning"
            )
        )
        self._task: asyncio.Task[None] | None = None

    @property
    def url(self) -> str:
        return f"ws://127.0.0.1:{self.port}/ws"

    @property
    def http(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    async def __aenter__(self) -> "Live":
        self._task = asyncio.create_task(self.server.serve())
        for _ in range(200):
            if self.server.started:
                return self
            await asyncio.sleep(0.02)
        raise RuntimeError("uvicorn did not start")

    async def __aexit__(self, *exc: object) -> None:
        self.server.should_exit = True
        self.server.force_exit = True
        if self._task is not None:
            await asyncio.wait_for(self._task, 10)

    @asynccontextmanager
    async def connect(
        self, *, origin: str | None = WEB_ORIGIN, cookie: str | None = None
    ) -> AsyncIterator["Client"]:
        headers = {"Cookie": cookie} if cookie else None
        async with connect(
            self.url,
            origin=None if origin is None else Origin(origin),
            additional_headers=headers,
            open_timeout=5,
            ping_interval=None,
        ) as ws:
            yield Client(ws)


class Client:
    def __init__(self, ws: ClientConnection) -> None:
        self.ws = ws

    async def send(self, **msg: Any) -> None:
        await self.ws.send(json.dumps(msg))

    async def recv(self, timeout: float = 5.0) -> dict[str, Any]:
        raw = await asyncio.wait_for(self.ws.recv(), timeout)
        data: dict[str, Any] = json.loads(raw)
        return data

    async def recv_type(self, type_: str, timeout: float = 5.0) -> dict[str, Any]:
        """Next frame of `type_`, skipping heartbeat pings."""
        while True:
            msg = await self.recv(timeout)
            if msg["type"] == type_:
                return msg
            if msg["type"] != "ping":
                raise AssertionError(f"expected {type_}, got {msg}")

    async def recv_data(self, n: int, timeout: float = 5.0) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        while len(out) < n:
            msg = await self.recv(timeout)
            if msg["type"] != "ping":
                out.append(msg)
        return out

    async def subscribe(
        self, *channels: str, resume: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        body: dict[str, Any] = {"op": "subscribe", "channels": list(channels), "id": "s1"}
        if resume is not None:
            body["resume"] = resume
        await self.send(**body)
        return await self.recv_type("subscribed")

    async def expect_close(self, timeout: float = 5.0) -> int:
        """Drain until the server closes; return the close code."""
        try:
            while True:
                await asyncio.wait_for(self.ws.recv(), timeout)
        except Exception as exc:  # websockets.ConnectionClosed
            rcvd = getattr(exc, "rcvd", None)
            if rcvd is None:
                raise
            return int(rcvd.code)


async def pub(h: Harness, channel: str, type_: str = "price", **data: Any) -> int:
    return (await publisher.publish(h.app.state.redis, channel, type_, data)).seq
