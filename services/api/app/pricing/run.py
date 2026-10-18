"""Run the pricing engine as its own process (it can move to its own container later).

    uv run python -m app.pricing.run

Set PRICING_EMBEDDED=false on the API so it does not also start one. It is safe to start
several: a Redis leader lock lets exactly one price while the others wait.
"""

import asyncio
import logging
import signal

import redis.asyncio as aioredis

from app.chain.client import ChainClient
from app.core.config import ConfigError, get_settings
from app.core.db import make_engine, make_sessionmaker
from app.core.logging import configure_logging
from app.pricing.factory import build_pricing

log = logging.getLogger("wattswap.pricing.run")


async def main() -> int:
    settings = get_settings()
    configure_logging(settings.api_log_level)
    if not settings.pricing_enabled:
        log.error("PRICING_ENABLED is false; nothing to run")
        return 1
    engine = make_engine(settings.database_url)
    redis = aioredis.from_url(settings.redis_url, decode_responses=True)
    chain: ChainClient | None
    try:
        chain = ChainClient(settings)
    except (ConfigError, FileNotFoundError) as exc:
        if settings.api_env == "production":
            raise
        log.warning("running without a chain connection: %s", exc)
        chain = None
    bundle = build_pricing(settings, make_sessionmaker(engine), redis, chain)

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:  # Windows event loops have no signal handlers
            signal.signal(sig, lambda *_: loop.call_soon_threadsafe(stop.set))
    log.info("pricing engine starting (standalone)")
    try:
        await bundle.service.run(stop)
    finally:
        await bundle.aclose()
        if chain is not None:
            await chain.close()
        await redis.aclose()
        await engine.dispose()
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(asyncio.run(main()))
