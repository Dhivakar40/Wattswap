"""Reduced fan-out load run for CI: many clients, one market, zero loss and ordered delivery.

The real latency budget is measured against the compose stack by `make ws-load`; here client and
server share one event loop on a CI runner, so only a very loose latency bound is asserted.
"""

import asyncio
import time

from tests.conftest import Harness, SettingsFactory
from tests.realtime.kit import Live, pub

CHANNEL = "loadtest:smoke"  # dedicated synthetic channel, no real market involved
CLIENTS = 100
MESSAGES = 60


async def test_100_clients_receive_every_message_in_order(make_settings: SettingsFactory) -> None:
    async with Harness(make_settings(ws_max_conn_per_ip=1000, ws_loadtest_enabled=True)) as h:
        async with Live(h) as lv:
            results: list[tuple[list[int], list[float]]] = []

            async def client() -> None:
                seqs: list[int] = []
                lat: list[float] = []
                async with lv.connect() as c:
                    await c.subscribe(CHANNEL)
                    ready.release()
                    while len(seqs) < MESSAGES:
                        m = await c.recv(15)
                        if m["type"] == "loadtest":
                            seqs.append(m["seq"])
                            lat.append((time.time_ns() - m["data"]["t_ns"]) / 1e6)
                results.append((seqs, lat))

            ready = asyncio.Semaphore(0)
            tasks = [asyncio.create_task(client()) for _ in range(CLIENTS)]
            for _ in range(CLIENTS):
                await asyncio.wait_for(ready.acquire(), 30)
            for i in range(MESSAGES):
                await pub(h, CHANNEL, "loadtest", n=i, t_ns=time.time_ns())
                await asyncio.sleep(0.02)
            await asyncio.wait_for(asyncio.gather(*tasks), 60)

    assert len(results) == CLIENTS
    for seqs, _ in results:
        assert seqs == list(range(1, MESSAGES + 1))
    all_lat = sorted(v for _, lat in results for v in lat)
    p95 = all_lat[int(len(all_lat) * 0.95) - 1]
    print(f"ws smoke: {CLIENTS} clients x {MESSAGES} msgs, p95 {p95:.0f} ms")
    assert p95 < 2000
