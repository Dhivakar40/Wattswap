"""Publishers for the owner-only `meter:{meterId}` channel.

Best effort: a Redis hiccup must never fail reading ingest or a mint, so errors are logged and
swallowed. Meters in v1 are the labeled simulator; every message carries `simulated` so a client
cannot present the data as hardware telemetry.
"""

import logging

import redis.asyncio as aioredis

from app.realtime import publisher
from app.schemas.api import MintOut, ReadingOut

log = logging.getLogger("wattswap.ws.meter")


def channel(meter_id: str) -> str:
    return f"meter:{meter_id.lower()}"


async def publish_reading(redis: aioredis.Redis, reading: ReadingOut, *, simulated: bool) -> None:
    try:
        data = reading.model_dump(mode="json")
        data["simulated"] = simulated
        await publisher.publish(redis, channel(reading.meter_id), "reading", data)
    except Exception:
        log.exception("could not publish reading", extra={"reading": reading.reading_id})


async def publish_mint(
    redis: aioredis.Redis, meter_id: str, reading_id: str, mint: MintOut, *, simulated: bool
) -> None:
    try:
        data = mint.model_dump(mode="json")
        data.update({"reading_id": reading_id, "meter_id": meter_id, "simulated": simulated})
        await publisher.publish(redis, channel(meter_id), "mint", data)
    except Exception:
        log.exception("could not publish mint", extra={"reading": reading_id})
