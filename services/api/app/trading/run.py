"""Run the trading workers (matcher, relayer, listener, sweeper) as their own process.

    uv run python -m app.trading.run

Set TRADING_EMBEDDED=false on the API so it does not also start them. Starting several is safe:
a Redis leader lock per role lets exactly one instance act while the others wait.
"""

import asyncio
import logging
import signal

from app.chain.client import ChainClient
from app.core.config import get_settings
from app.core.db import make_engine, make_sessionmaker
from app.core.logging import configure_logging
from app.core.redis import make_redis
from app.workers.trading import build_trading, validate

log = logging.getLogger("wattswap.trading.run")


async def main() -> int:
    settings = get_settings()
    configure_logging(settings.api_log_level)
    if not settings.trading_enabled:
        log.error("TRADING_ENABLED is false; nothing to run")
        return 1
    engine = make_engine(settings.database_url)
    redis = make_redis(settings)
    client = ChainClient(settings)
    bundle = build_trading(settings, make_sessionmaker(engine), redis, client)
    problems = await validate(settings, client, bundle.relayer.sender.address)
    for problem in problems:
        log.error("trading config check failed: %s", problem)
    if problems:
        return 2
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:  # Windows event loops have no signal handlers
            signal.signal(sig, lambda *_: loop.call_soon_threadsafe(stop.set))
    log.info("trading workers starting (standalone)")
    try:
        await bundle.run(stop)
    finally:
        await client.close()
        await redis.aclose()
        await engine.dispose()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
