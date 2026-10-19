"""One way to build the Redis client, so every process behaves the same under load."""

import redis.asyncio as aioredis

from app.core.config import Settings


def make_redis(settings: Settings) -> aioredis.Redis:
    """A client whose pool WAITS for a free connection instead of failing.

    The redis-py default is a non-blocking pool capped at 100: a burst of concurrent WebSocket
    subscribes (each reads the channel head) would raise `MaxConnectionsError` and be dropped.
    Blocking turns that burst into a short queue; `redis_max_connections` bounds the memory."""
    pool = aioredis.BlockingConnectionPool.from_url(
        settings.redis_url,
        decode_responses=True,
        max_connections=settings.redis_max_connections,
        timeout=settings.redis_pool_wait_seconds,
    )
    return aioredis.Redis(connection_pool=pool)
