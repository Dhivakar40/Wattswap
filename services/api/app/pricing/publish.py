"""Publish ticks to Redis: pub/sub for live consumers, keys for snapshots.

Channel `price:{marketId}` carries the envelope the WebSocket gateway forwards unchanged:
{type, channel, seq, ts, data}. `seq` is allocated atomically with the publish
(`app.realtime.publisher`) so a client can detect gaps and re-snapshot or resume.
"""

import json
from typing import Any

import redis.asyncio as aioredis

from app.pricing.engine import Tick
from app.pricing.quotes import SignedQuote
from app.realtime import channels as rt_channels
from app.realtime import publisher


def channel(market_id: str) -> str:
    return f"price:{market_id}"


def latest_key(market_id: str) -> str:
    return rt_channels.latest_key(channel(market_id))


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


Published = publisher.Published


async def publish_tick(
    redis: aioredis.Redis,
    tick: Tick,
    market_id: str,
    quote: SignedQuote | None,
    clock_mode: str,
) -> Published:
    data = tick_payload(tick, market_id, quote, clock_mode)
    published = await publisher.publish(
        redis, channel(market_id), "price", data, ts=tick.ts, keep_latest=True
    )
    if quote is not None:
        await redis.set(quote_key(market_id), json.dumps(quote.to_json(), separators=(",", ":")))
    return published
