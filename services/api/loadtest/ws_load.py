"""Load test for the /ws gateway: N concurrent clients on one channel, delivery latency and loss.

    make ws-load ARGS="--clients 500 --messages 200 --rate 10"
    make ws-load ARGS="--api http://localhost:8000,http://localhost:8001 --clients 500"   # 2 replicas

DEV STACK ONLY: it publishes synthetic messages to a dedicated `loadtest:run-<timestamp>` channel
(the gateway must run with WS_LOADTEST_ENABLED=true, which it refuses in production). No real market,
order book or UI channel is touched, so nothing a user could see is affected.

What is measured. A publisher process writes messages through the production publisher (so seq,
retention and fan-out are the real thing), each carrying `t_ns` = wall-clock ns at publish. A client
records its own wall-clock ns when the frame is read. Latency = receive - publish, which spans
Redis -> gateway hub -> per-connection queue -> socket -> this client. Publisher, gateways and
clients share one machine and one clock here, so there is no clock skew, but they also share one
CPU: absolute numbers are indicative, and the client side is Python. The report therefore includes
the client event-loop lag (how late a 10 ms timer fires): if that is large, the latency numbers are
inflated by this tool, not by the server.

Pass/fail: zero lost messages, zero duplicates or out-of-order seqs, every client connected, and
p95 latency within --budget-ms.
"""

import argparse
import asyncio
import itertools
import json
import math
import os
import statistics
import subprocess
import sys
import time
from dataclasses import dataclass, field

import redis.asyncio as aioredis
from websockets.asyncio.client import connect
from websockets.typing import Origin

from app.realtime import channels as rt_channels
from app.realtime import publisher


@dataclass
class ClientStats:
    latencies_ns: list[int] = field(default_factory=list)
    seqs: list[int] = field(default_factory=list)
    connected: bool = False
    close_code: int | None = None
    error: str | None = None


def pct(sorted_values: list[float], p: float) -> float:
    if not sorted_values:
        return math.nan
    k = min(len(sorted_values) - 1, max(0, math.ceil(p / 100 * len(sorted_values)) - 1))
    return sorted_values[k]


async def run_client(
    url: str,
    origin: str,
    channels: list[str],
    load_channel: str,
    stats: ClientStats,
    connected: asyncio.Semaphore,
    done: asyncio.Event,
) -> None:
    try:
        async with connected:  # bound simultaneous handshakes
            ws = await connect(
                url, origin=Origin(origin), ping_interval=None, open_timeout=30, max_size=2**20
            )
        await ws.send(json.dumps({"op": "subscribe", "channels": channels}))
        while True:
            frame = json.loads(await ws.recv())
            if frame["type"] == "subscribed":
                break
        stats.connected = True
        marker = '"t_ns":'
        while not done.is_set():
            try:
                raw = await asyncio.wait_for(ws.recv(), 0.5)
            except TimeoutError:
                continue
            now = time.time_ns()
            text = raw if isinstance(raw, str) else raw.decode()
            if text.startswith('{"type":"ping"'):
                await ws.send('{"op":"pong"}')
                continue
            if f'"channel":"{load_channel}"' not in text or marker not in text:
                continue
            i = text.index(marker) + len(marker)
            t_ns = int(text[i : text.index("}", i)])
            seq = int(text[text.index('"seq":') + 6 : text.index(',"ts"')])
            stats.seqs.append(seq)
            stats.latencies_ns.append(now - t_ns)
        await ws.close()
    except Exception as exc:
        stats.error = f"{type(exc).__name__}: {exc}"
        rcvd = getattr(exc, "rcvd", None)
        if rcvd is not None:
            stats.close_code = int(rcvd.code)


async def lag_monitor(samples: list[float], stop: asyncio.Event) -> None:
    while not stop.is_set():
        t0 = time.perf_counter()
        await asyncio.sleep(0.01)
        samples.append(max(0.0, (time.perf_counter() - t0 - 0.01) * 1000))


def docker_stats(names: list[str]) -> str:
    try:
        out = subprocess.run(
            [
                "docker",
                "stats",
                "--no-stream",
                "--format",
                "{{.Name}}: cpu {{.CPUPerc}} mem {{.MemUsage}}",
                *names,
            ],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return "(docker stats unavailable)"
    return out or "(docker stats unavailable)"


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--api", default="http://localhost:8000", help="comma-separated base URLs")
    ap.add_argument("--clients", type=int, default=500)
    ap.add_argument("--messages", type=int, default=200)
    ap.add_argument("--rate", type=float, default=10.0, help="published messages per second")
    ap.add_argument("--payload-bytes", type=int, default=1500, help="approx. size of each message")
    ap.add_argument("--budget-ms", type=float, default=250.0, help="p95 latency budget")
    ap.add_argument("--origin", default="http://localhost:3000")
    ap.add_argument("--redis", default=os.environ.get("REDIS_URL", "redis://localhost:6379/0"))
    ap.add_argument("--handshakes", type=int, default=50, help="concurrent handshakes")
    ap.add_argument("--containers", default="wattswap-api-1,wattswap-api2-1")
    ap.add_argument("--force", action="store_true", help="allow a non-local Redis")
    args = ap.parse_args()

    if not args.force and not any(h in args.redis for h in ("localhost", "127.0.0.1")):
        print("refusing to publish synthetic messages to a non-local Redis (use --force)")
        return 2
    apis = [a.strip().rstrip("/") for a in args.api.split(",") if a.strip()]
    # A dedicated synthetic channel: no real market, order book or UI ever sees these messages.
    load_channel = f"loadtest:run-{int(time.time())}"
    channels = [load_channel]
    urls = [a.replace("http", "ws", 1) + "/ws" for a in apis]

    redis = aioredis.from_url(args.redis, decode_responses=True)
    done = asyncio.Event()
    handshakes = asyncio.Semaphore(args.handshakes)
    stats = [ClientStats() for _ in range(args.clients)]
    t_connect = time.perf_counter()
    tasks = [
        asyncio.create_task(
            run_client(
                urls[i % len(urls)], args.origin, channels, load_channel, s, handshakes, done
            )
        )
        for i, s in enumerate(stats)
    ]
    while sum(s.connected or s.error is not None for s in stats) < args.clients:
        await asyncio.sleep(0.2)
        if time.perf_counter() - t_connect > 120:
            break
    connect_s = time.perf_counter() - t_connect
    ok = sum(s.connected for s in stats)
    print(
        f"connected {ok}/{args.clients} clients in {connect_s:.1f}s across {len(urls)} replica(s)"
    )
    if ok == 0:
        for s in stats[:3]:
            print("  error:", s.error)
        return 1

    lag: list[float] = []
    lag_stop = asyncio.Event()
    lag_task = asyncio.create_task(lag_monitor(lag, lag_stop))
    pad = "x" * max(0, args.payload_bytes - 80)
    first_seq: int | None = None
    mid_stats = "n/a"
    started = time.perf_counter()
    interval = 1.0 / args.rate
    for i in range(args.messages):
        target = started + i * interval
        await asyncio.sleep(max(0.0, target - time.perf_counter()))
        p = await publisher.publish(
            redis, load_channel, "loadtest", {"n": i, "pad": pad, "t_ns": time.time_ns()}
        )
        first_seq = p.seq if first_seq is None else first_seq
        if i == args.messages // 2:
            mid_stats = await asyncio.to_thread(
                docker_stats, args.containers.split(",")
            )  # off-loop
    await asyncio.sleep(3)  # drain
    done.set()
    lag_stop.set()
    await asyncio.gather(*tasks, lag_task, return_exceptions=True)
    await redis.delete(  # leave nothing behind: the channel is synthetic
        rt_channels.seq_key(load_channel),
        rt_channels.stream_key(load_channel),
        rt_channels.latest_key(load_channel),
        rt_channels.epoch_key(load_channel),
    )
    await redis.aclose()

    expected = args.messages
    lat_ms = sorted(ns / 1e6 for s in stats for ns in s.latencies_ns)
    lost = duplicates = disorder = 0
    for s in stats:
        if not s.connected:
            continue
        got = s.seqs
        want = list(range(first_seq or 0, (first_seq or 0) + expected))
        lost += len(set(want) - set(got))
        duplicates += len(got) - len(set(got))
        disorder += sum(1 for a, b in itertools.pairwise(got) if b <= a)
    delivered = len(lat_ms)
    errors = [s.error for s in stats if s.error]
    lag.sort()
    print(
        f"published {expected} msgs @ {args.rate}/s, ~{args.payload_bytes} B each, "
        f"to {ok} clients -> {delivered} deliveries ({delivered / max(1, time.perf_counter() - started):.0f}/s)"
    )
    print(
        f"latency ms  p50 {pct(lat_ms, 50):.1f}  p95 {pct(lat_ms, 95):.1f}  "
        f"p99 {pct(lat_ms, 99):.1f}  max {lat_ms[-1] if lat_ms else math.nan:.1f}  "
        f"mean {statistics.fmean(lat_ms) if lat_ms else math.nan:.1f}"
    )
    print(
        f"client loop lag ms (inflates the above)  p50 {pct(lag, 50):.1f}  p95 {pct(lag, 95):.1f}  max {lag[-1] if lag else math.nan:.1f}"
    )
    print(
        f"lost {lost}  duplicates {duplicates}  out-of-order {disorder}  client errors {len(errors)}"
    )
    print(f"server (mid-run): {mid_stats}")
    if errors:
        print("  first error:", errors[0])
    p95 = pct(lat_ms, 95)
    passed = (
        ok == args.clients
        and lost == 0
        and duplicates == 0
        and disorder == 0
        and p95 <= args.budget_ms
    )
    print(f"budget p95 <= {args.budget_ms:.0f} ms  ->  {'PASS' if passed else 'FAIL'}")
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
