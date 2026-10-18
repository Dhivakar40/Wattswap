"""Publish ticks to Redis: pub/sub for live consumers, keys for snapshots.

Channel `price:{marketId}` carries the envelope the Phase 5 WebSocket gateway forwards
unchanged: {type, channel, seq, ts, data}. `seq` is a per-market counter so a client can
detect gaps and re-snapshot over REST.
"""

import json
from dataclasses import dataclass
from typing import Any

import redis.asyncio as aioredis

from app.pricing.engine import Tick
from app.pricing.quotes import SignedQuote


def channel(market_id: str) -> str:
    return f"price:{market_id}"


def latest_key(market_id: str) -> str:
    return f"price:{market_id}:latest"


def quote_key(market_id: str) -> str:
    return f"quote:{market_id}:latest"


def state_key(market_id: str) -> str:
    return f"pricing:state:{market_id}"


def forecast_key(market_id: str) -> str:
    return f"pricing:forecast:{market_id}"


def command_key(market_id: str) -> str:
    return f"pricing:cmd:{market_id}"


def tick_payload(
    tick: Tick, market_id: str, quote: SignedQuote | None, clock_mode: str
) -> dict[str, Any]:
    """The JSON a consumer sees: only ints and strings (no floats)."""
    return {
        "marketId": market_id,
        "price": tick.price,
        "rawPrice": tick.raw_price,
        "emaPrice": str(tick.ema_price),
        "supplyW": tick.supply_w,
        "demandW": tick.demand_w,
        "bandBps": tick.band_bps,
        "breaker": tick.breaker.mode.value,
        "breakerReason": tick.breaker.reason,
        "weatherStale": tick.weather_stale,
        "halted": tick.halted,
        "volBps": None if tick.vol_bps is None else f"{tick.vol_bps:.4f}",
        "ts": tick.ts,
        "marketTime": tick.market_time,
        "clockMode": clock_mode,
        "accelerated": tick.market_time < tick.ts - 60,
        "quote": None if quote is None else quote.to_json(),
        "drivers": tick.drivers,
        "events": list(tick.events),
    }


@dataclass(frozen=True)
class Published:
    seq: int
    receivers: int


async def publish_tick(
    redis: aioredis.Redis,
    tick: Tick,
    market_id: str,
    quote: SignedQuote | None,
    clock_mode: str,
) -> Published:
    seq = int(await redis.incr(f"{channel(market_id)}:seq"))
    data = tick_payload(tick, market_id, quote, clock_mode)
    envelope = {
        "type": "price",
        "channel": channel(market_id),
        "seq": seq,
        "ts": tick.ts,
        "data": data,
    }
    body = json.dumps(envelope, separators=(",", ":"))
    async with redis.pipeline(transaction=False) as pipe:
        pipe.set(latest_key(market_id), body)
        if quote is not None:
            pipe.set(quote_key(market_id), json.dumps(quote.to_json(), separators=(",", ":")))
        pipe.publish(channel(market_id), body)
        results = await pipe.execute()
    return Published(seq, int(results[-1]))
