"""DEMO (testnet/Anvil only): prove /ws fan-out across two API replicas.

    make up && make deploy-local && make sim          # stack, contracts, simulated meter
    make ws-fanout-demo                               # this script (starts the 2nd replica)

Client A is connected to replica 1 (:8000), client B to replica 2 (:8001). Both subscribe to the
same market channels. A trade is then driven THROUGH REPLICA 2's REST API (dev.trade_demo). Every
message is published to Redis by whichever process produced it; both gateways forward it. The
script passes only if A and B received identical (channel, seq) sequences with no gap and no
duplicate, and prints what each saw.
"""

import argparse
import asyncio
import json
import sys
from collections import defaultdict
from typing import Any

import httpx
from websockets.asyncio.client import connect
from websockets.typing import Origin


async def watch(
    name: str, url: str, channels: list[str], seen: list[tuple[str, int, str]], ready: asyncio.Event
) -> None:
    async with connect(url, origin=Origin("http://localhost:3000"), ping_interval=None) as ws:
        await ws.send(json.dumps({"op": "subscribe", "channels": channels}))
        async for raw in ws:
            msg: dict[str, Any] = json.loads(raw)
            if msg["type"] == "subscribed":
                ready.set()
            elif msg["type"] == "ping":
                await ws.send(json.dumps({"op": "pong"}))
            elif "seq" in msg:
                seen.append((msg["channel"], msg["seq"], msg["type"]))


def gapless(seen: list[tuple[str, int, str]]) -> bool:
    per: dict[str, list[int]] = defaultdict(list)
    for channel, seq, _ in seen:
        per[channel].append(seq)
    return all(seqs == list(range(seqs[0], seqs[0] + len(seqs))) for seqs in per.values())


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--api1", default="http://localhost:8000")
    ap.add_argument("--api2", default="http://localhost:8001")
    ap.add_argument("--wh", default="500")
    args = ap.parse_args()

    async with httpx.AsyncClient() as http:
        markets = (await http.get(f"{args.api1}/api/v1/markets")).json()
    market = next(m for m in markets if m.get("is_primary")) if markets else None
    if market is None:
        print("no market found on replica 1", file=sys.stderr)
        return 2
    mid = market["market_id"]
    channels = [f"orderbook:{mid}", f"trades:{mid}", f"price:{mid}"]
    ws1 = args.api1.replace("http", "ws", 1) + "/ws"
    ws2 = args.api2.replace("http", "ws", 1) + "/ws"

    seen_a: list[tuple[str, int, str]] = []
    seen_b: list[tuple[str, int, str]] = []
    ready_a, ready_b = asyncio.Event(), asyncio.Event()
    tasks = [
        asyncio.create_task(watch("A", ws1, channels, seen_a, ready_a)),
        asyncio.create_task(watch("B", ws2, channels, seen_b, ready_b)),
    ]
    await asyncio.wait_for(asyncio.gather(ready_a.wait(), ready_b.wait()), 10)
    print(f"A -> {ws1}\nB -> {ws2}\ndriving a trade through replica 2 ({args.api2}) ...")

    proc = await asyncio.create_subprocess_exec(
        sys.executable, "-m", "dev.trade_demo", "--api", args.api2, "--wh", args.wh
    )
    code = await proc.wait()
    await asyncio.sleep(2)  # let the last messages drain
    for t in tasks:
        t.cancel()

    # compare only the trade-driven channels: price ticks may land between the two subscriptions
    def trade_driven(seen: list[tuple[str, int, str]]) -> list[tuple[str, int]]:
        return [(c, q) for c, q, _ in seen if not c.startswith("price:")]

    a, b = trade_driven(seen_a), trade_driven(seen_b)
    print(f"\n{'channel':<22}{'type':<12}{'seq':>5}   A   B")
    for channel, seq, type_ in seen_a:
        if channel.startswith("price:"):
            continue
        print(
            f"{channel.split(':')[0]:<22}{type_:<12}{seq:>5}   x   {'x' if (channel, seq) in b else '-'}"
        )
    ok = code == 0 and bool(a) and a == b and gapless(seen_a) and gapless(seen_b)
    print(
        f"\ntrade_demo exit={code}  A saw {len(a)} msgs, B saw {len(b)}  ->  {'PASS' if ok else 'FAIL'}"
    )
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
