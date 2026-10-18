"""Funding sweeper: auto-cancels orders whose maker can no longer honour them.

A maker's total commitment must fit balance AND allowance (see `app.matching.snapshot`). Funds can
vanish at any moment (a transfer, a revoked approval), so:
  * every maker with open orders is re-checked each `funding_sweep_seconds`;
  * makers the chain listener saw moving funds (Transfer/Approval) are re-checked on the next tick.
Over-committed makers lose their NEWEST orders first, so older orders keep their time priority.
"""

import logging
import time

import redis.asyncio as aioredis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.chain.client import ChainClient
from app.core.config import Settings
from app.matching import lifecycle, snapshot
from app.matching.events import Outbox
from app.models import Order
from app.workers.listener import DIRTY_KEY

log = logging.getLogger("wattswap.sweeper")


class FundingSweeper:
    def __init__(
        self,
        settings: Settings,
        sessionmaker: async_sessionmaker[AsyncSession],
        redis: aioredis.Redis,
        client: ChainClient,
    ) -> None:
        self.settings = settings
        self.sessionmaker = sessionmaker
        self.redis = redis
        self.client = client
        self._last_full = 0.0

    async def run_once(self, *, full: bool | None = None) -> set[str]:
        """Returns the order hashes cancelled in this pass."""
        due = (time.monotonic() - self._last_full) >= self.settings.funding_sweep_seconds
        full = due if full is None else full
        dirty = {a.lower() for a in await self.redis.smembers(DIRTY_KEY)}
        if dirty:
            await self.redis.srem(DIRTY_KEY, *dirty)
        chain_now = await self.client.latest_block_timestamp()
        async with self.sessionmaker() as s:
            rows = await s.scalars(
                select(Order.maker).where(Order.status.in_(lifecycle.ACTIVE)).distinct()
            )
            with_orders = set(rows.all())
        targets = with_orders if full else with_orders & dirty
        if full:
            self._last_full = time.monotonic()
        cancelled: set[str] = set()
        for maker in sorted(targets):
            out = Outbox()
            async with self.sessionmaker() as s, s.begin():
                done, _ = await snapshot.enforce(s, self.client, maker, out, chain_now)
            cancelled |= done
            await out.flush(self.redis, self.sessionmaker)
        return cancelled
