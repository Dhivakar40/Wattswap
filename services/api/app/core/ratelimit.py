"""Redis fixed-window rate limiting."""

from collections.abc import Awaitable, Callable

import redis.asyncio as aioredis
from fastapi import Request

from app.core.config import Settings
from app.core.errors import AppError


async def hit(redis: aioredis.Redis, key: str, limit: int, window_seconds: int) -> None:
    """Count one request against `key`; raise 429 once `limit` is exceeded in the window."""
    full = f"rl:{key}"
    async with redis.pipeline(transaction=True) as pipe:
        pipe.incr(full)
        pipe.expire(full, window_seconds, nx=True)
        pipe.ttl(full)
        count, _, ttl = await pipe.execute()
    if int(count) > limit:
        retry = max(int(ttl), 1)
        raise AppError(
            429,
            "rate_limited",
            "too many requests",
            {"limit": limit, "window_seconds": window_seconds, "retry_after": retry},
            headers={"Retry-After": str(retry)},
        )


def client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def ip_limit(bucket: str, attr: str = "rate_limit_ip") -> Callable[[Request], Awaitable[None]]:
    """Dependency factory: limit by client IP using the named Settings attribute."""

    async def dependency(request: Request) -> None:
        settings: Settings = request.app.state.settings
        redis: aioredis.Redis = request.app.state.redis
        await hit(
            redis,
            f"{bucket}:ip:{client_ip(request)}",
            int(getattr(settings, attr)),
            settings.rate_limit_window_seconds,
        )

    return dependency
