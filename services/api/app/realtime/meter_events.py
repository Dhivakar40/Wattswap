"""Publishers for the owner-only `meter:{meterId}` channel (and mint status on `user:{owner}`).

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


def user_channel(owner_address: str) -> str:
    return f"user:{owner_address.lower()}"


async def publish_mint(
    redis: aioredis.Redis,
    meter_id: str,
    reading_id: str,
    mint: MintOut,
    *,
    simulated: bool,
    owner: str | None = None,
) -> None:
    """Announce a mint status change on `meter:{id}` and, when the owner is known, on `user:{owner}`
    too, so a dashboard that only follows the user channel sees every mint (`produced` is a mint total).
    The two publishes are independent: a failure on one never blocks the other."""
    data = mint.model_dump(mode="json")
    data.update({"reading_id": reading_id, "meter_id": meter_id, "simulated": simulated})
    targets = [channel(meter_id)] + ([user_channel(owner)] if owner else [])
    for target in targets:
        try:
            await publisher.publish(redis, target, "mint", data)
        except Exception:
            log.exception(
                "could not publish mint", extra={"reading": reading_id, "channel": target}
            )
