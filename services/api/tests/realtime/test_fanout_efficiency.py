"""A broadcast is serialized once and the same string object goes to every client."""

import asyncio
import json
from typing import Any

import pytest

from app.realtime import connection as connection_module
from tests.conftest import Harness, SettingsFactory
from tests.realtime.kit import WEB_ORIGIN, Live

CHANNEL = "loadtest:efficiency"
MARKER = "single-serialization-marker"
CLIENTS = 50


async def test_one_parse_and_one_shared_string_for_all_clients(
    make_settings: SettingsFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with Harness(make_settings(ws_loadtest_enabled=True, ws_max_conn_per_ip=1000)) as h:
        async with Live(h) as lv:
            clients = []
            for _ in range(CLIENTS):
                cm = lv.connect(origin=WEB_ORIGIN)
                clients.append((cm, await cm.__aenter__()))
            try:
                for _, c in clients:
                    await c.subscribe(CHANNEL)

                seen_ids: list[int] = []
                original_enqueue = connection_module.Connection.enqueue

                def spy(self: Any, raw: str, kind: str = "data") -> None:
                    if MARKER in raw:
                        seen_ids.append(id(raw))
                    original_enqueue(self, raw, kind)

                monkeypatch.setattr(connection_module.Connection, "enqueue", spy)

                parses: list[str] = []
                real_loads = json.loads

                def counting_loads(s: Any, *a: Any, **kw: Any) -> Any:
                    if isinstance(s, str) and MARKER in s:
                        parses.append(s)
                    return real_loads(s, *a, **kw)

                monkeypatch.setattr(json, "loads", counting_loads)

                from app.realtime import publisher

                await publisher.publish(h.app.state.redis, CHANNEL, "loadtest", {"m": MARKER})
                frames = [await asyncio.wait_for(c.ws.recv(), 5) for _, c in clients]
                monkeypatch.undo()  # the client below parses; the count above is already taken
            finally:
                for cm, _ in clients:
                    await cm.__aexit__(None, None, None)

    assert len(parses) == 1, "the hub must parse the envelope once per message, not per client"
    assert len(seen_ids) == CLIENTS and len(set(seen_ids)) == 1, (
        "every client must be handed the very same string object (no per-client re-serialization)"
    )
    assert len(set(frames)) == 1 and MARKER in str(frames[0])
