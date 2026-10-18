"""Assemble the trading workers (matcher, relayer, listener, sweeper) and run them as leaders.

Used by the API lifespan (embedded) and by `python -m app.trading.run` (standalone). Every role is
a Redis leader loop, so any number of instances can run and exactly one of each role is active.
"""

import asyncio
import logging
from dataclasses import dataclass

import redis.asyncio as aioredis
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.chain.client import ChainClient
from app.chain.tx import TxSender
from app.core.config import Settings
from app.matching.matcher import Matcher
from app.workers.leader import LeaderLoop
from app.workers.listener import Listener
from app.workers.relayer import Relayer
from app.workers.sweeper import FundingSweeper

log = logging.getLogger("wattswap.trading")


@dataclass
class TradingBundle:
    matcher: Matcher
    relayer: Relayer
    listener: Listener
    sweeper: FundingSweeper
    loops: dict[str, LeaderLoop]

    async def run(self, stop: asyncio.Event) -> None:
        await asyncio.gather(*(loop.run(stop) for loop in self.loops.values()))


async def validate(settings: Settings, client: ChainClient, relayer_address: str) -> list[str]:
    """Configuration that would make trading silently fail. Empty = fine."""
    problems: list[str] = []
    role = await client.exchange.functions.RELAYER_ROLE().call()
    if not await client.exchange.functions.hasRole(role, relayer_address).call():
        problems.append(f"relayer {relayer_address} lacks RELAYER_ROLE on the exchange")
    params = await client.exchange_params(fresh=True)
    if not 0 < settings.quote_safety_margin_seconds < params.max_staleness:
        problems.append(
            f"QUOTE_SAFETY_MARGIN_SECONDS={settings.quote_safety_margin_seconds} must be between 0 "
            f"and the oracle's maxStaleness ({params.max_staleness}s)"
        )
    return problems


def build_trading(
    settings: Settings,
    sessionmaker: async_sessionmaker[AsyncSession],
    redis: aioredis.Redis,
    client: ChainClient,
    *,
    suffix: str = "",
) -> TradingBundle:
    """`suffix` namespaces the lock names (tests run several isolated bundles side by side)."""
    key = settings.relayer_private_key
    if key is None:
        raise ValueError("RELAYER_PRIVATE_KEY is required for trading")
    sender = TxSender(client, client.account(key.get_secret_value()), redis, "relayer")
    matcher = Matcher(settings, sessionmaker, redis, client)
    relayer = Relayer(settings, sessionmaker, redis, client, sender)
    listener = Listener(settings, sessionmaker, redis, client)
    sweeper = FundingSweeper(settings, sessionmaker, redis, client)
    ttl = settings.trading_leader_ttl_seconds

    def loop(name: str, interval: float, step: object) -> LeaderLoop:
        return LeaderLoop(
            redis,
            name + suffix,
            ttl_seconds=ttl,
            interval_seconds=interval,
            step=step,  # type: ignore[arg-type]
        )

    loops = {
        "matcher": loop("matcher", settings.match_poll_ms / 1000, matcher.run_once),
        "relayer": loop("relayer", settings.relayer_poll_seconds, relayer.run_once),
        "listener": loop("listener", settings.listener_poll_seconds, listener.run_once),
        "sweeper": loop("sweeper", 1.0, sweeper.run_once),
    }
    matcher.still_leader = loops["matcher"].owned
    return TradingBundle(matcher, relayer, listener, sweeper, loops)
