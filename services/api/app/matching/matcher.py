"""The matcher: price-time priority, single writer.

One cycle = one DB transaction per market:
  1. housekeeping (expire, close dust remainders),
  2. lock the market's matchable orders FOR UPDATE (Postgres serialises any second writer here),
  3. run the pure `match_book`,
  4. create `trade` + `settlement(matched)` rows and reserve Wh on both orders,
  5. re-check that the Redis leader lock is still ours, then commit.

Defence in depth against two writers (lock lost during a pause, clock skew): steps 2 and 5 plus the
`filled_wh + reserved_wh <= amount_wh` CHECK make a double reservation impossible.
"""

import json
import logging
import time
import uuid
from collections import Counter
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from decimal import Decimal

import redis.asyncio as aioredis
from redis.exceptions import LockError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.chain.client import ChainClient
from app.core.config import Settings
from app.core.metrics import MATCH_CYCLE, MATCH_SKIPPED, MATCHES, SETTLEMENTS
from app.matching import lifecycle
from app.matching.book import BookOrder, MatchContext, match_book
from app.matching.events import Outbox
from app.models import (
    Market,
    Order,
    OrderSide,
    OrderStatus,
    Settlement,
    SettlementStatus,
    Trade,
)
from app.pricing import publish as price_publish
from app.schemas.trading import order_out

log = logging.getLogger("wattswap.matcher")

PAUSE_KEY = "trading:paused"


@dataclass
class Quote:
    price: int
    timestamp: int
    signature: str = ""


@dataclass
class CycleResult:
    matched: int = 0
    skipped: Counter[str] = field(default_factory=Counter)
    idle_reason: str | None = None  # why no matching happened this cycle
    expired: int = 0
    dust_closed: int = 0


async def load_quote(redis: aioredis.Redis, market_id: str) -> Quote | None:
    raw = await redis.get(price_publish.quote_key(market_id))
    if not raw:
        return None
    try:
        body = json.loads(raw)
        return Quote(int(body["price"]), int(body["timestamp"]), str(body.get("signature", "")))
    except (ValueError, KeyError, TypeError):
        return None


def quote_is_fresh(q: Quote, chain_now: int, max_staleness: int, margin: int) -> bool:
    """Usable for matching: not from the future and at least `margin` seconds of validity left."""
    return q.timestamp <= chain_now and chain_now - q.timestamp <= max_staleness - margin


class Matcher:
    def __init__(
        self,
        settings: Settings,
        sessionmaker: async_sessionmaker[AsyncSession],
        redis: aioredis.Redis,
        client: ChainClient,
        *,
        still_leader: Callable[[], Awaitable[bool]] | None = None,
    ) -> None:
        self.settings = settings
        self.sessionmaker = sessionmaker
        self.redis = redis
        self.client = client
        self.still_leader = still_leader
        self.last_result = CycleResult()

    async def run_once(self) -> CycleResult:
        started = time.monotonic()
        result = CycleResult()
        if await self.redis.get(PAUSE_KEY):
            result.idle_reason = "paused_by_admin"
            self.last_result = result
            return result
        chain_now = await self.client.latest_block_timestamp()
        params = await self.client.exchange_params()
        async with self.sessionmaker() as s:
            markets = list((await s.scalars(select(Market).where(Market.enabled.is_(True)))).all())
        for market in markets:
            await self._cycle_market(
                market.market_id,
                chain_now,
                params.band_bps,
                params.fee_bps,
                params.max_staleness,
                params.paused,
                result,
            )
        MATCH_CYCLE.observe(time.monotonic() - started)
        self.last_result = result
        return result

    async def _cycle_market(
        self,
        market_id: str,
        chain_now: int,
        band_bps: int,
        fee_bps: int,
        max_staleness: int,
        paused: bool,
        result: CycleResult,
    ) -> None:
        cfg = self.settings
        out = Outbox()
        async with self.sessionmaker() as s:
            async with s.begin():
                expired, dust = await self._housekeeping(s, market_id, chain_now, out)
                result.expired += expired
                result.dust_closed += dust

                quote = await load_quote(self.redis, market_id)
                if paused:
                    result.idle_reason = "exchange_paused"
                elif quote is None:
                    result.idle_reason = "no_quote"
                elif not quote_is_fresh(
                    quote, chain_now, max_staleness, cfg.quote_safety_margin_seconds
                ):
                    result.idle_reason = "stale_quote"
                if result.idle_reason is not None or quote is None:
                    MATCH_SKIPPED.labels(result.idle_reason or "no_quote").inc()
                else:
                    await self._match(
                        s, market_id, quote, chain_now, band_bps, fee_bps, out, result
                    )
                if self.still_leader is not None and not await self.still_leader():
                    raise LockError("leader lock lost mid-cycle; rolling back")
        await out.flush(self.redis, self.sessionmaker)

    async def _housekeeping(
        self, s: AsyncSession, market_id: str, chain_now: int, out: Outbox
    ) -> tuple[int, int]:
        cfg = self.settings
        rows = await s.scalars(
            select(Order)
            .where(Order.market_id == market_id, Order.status.in_(lifecycle.ACTIVE))
            .order_by(Order.id)
            .with_for_update()
        )
        expired = dust = 0
        for o in rows:
            if o.expiry <= chain_now:
                if lifecycle.reserved(o) == 0:
                    o.status = OrderStatus.EXPIRED
                    o.cancel_pending = False
                    out.order(order_out(o))
                    expired += 1
                continue
            unfilled = lifecycle.amount(o) - lifecycle.filled(o)
            if (
                0 < unfilled < cfg.min_fill_wh
                and lifecycle.reserved(o) == 0
                and not o.cancel_pending
            ):
                # A leftover too small to ever settle: close it, keep the reason for the UI.
                await lifecycle.cancel_order(s, o, "dust_remainder", "offchain", out)
                dust += 1
        return expired, dust

    async def _match(
        self,
        s: AsyncSession,
        market_id: str,
        quote: Quote,
        chain_now: int,
        band_bps: int,
        fee_bps: int,
        out: Outbox,
        result: CycleResult,
    ) -> None:
        cfg = self.settings
        rows = list(
            await s.scalars(
                select(Order)
                .where(
                    Order.market_id == market_id,
                    Order.status.in_(lifecycle.ACTIVE),
                    Order.cancel_pending.is_(False),
                    Order.expiry > chain_now + cfg.expiry_margin_seconds,
                )
                .order_by(Order.id)
                .with_for_update()
            )
        )
        by_key = {str(o.id): o for o in rows}

        def book(side: OrderSide) -> list[BookOrder]:
            return [
                BookOrder(
                    key=str(o.id),
                    order_hash=o.order_hash,
                    maker=o.maker,
                    limit_price=int(o.limit_price),
                    seq=o.seq,
                    matchable_wh=lifecycle.matchable(o),
                )
                for o in rows
                if o.side == side and lifecycle.matchable(o) > 0
            ]

        ctx = MatchContext(
            reference_price=quote.price,
            quote_price=quote.price,
            onchain_band_bps=band_bps,
            band_safety_pct=cfg.band_safety_pct,
            fee_bps=fee_bps,
            min_fill_cost=cfg.min_fill_cost,
            cooled_pairs=await lifecycle.cooled_pairs(s),
        )
        matches, skipped = match_book(book(OrderSide.BUY), book(OrderSide.SELL), ctx)
        for reason, n in skipped.items():
            MATCH_SKIPPED.labels(reason).inc(n)
        result.skipped.update(skipped)

        for m in matches:
            buy, sell = by_key[m.bid.key], by_key[m.ask.key]
            fill = Decimal(m.fill_wh)
            trade = Trade(
                id=uuid.uuid4(),
                market_id=market_id,
                buy_order_id=buy.id,
                sell_order_id=sell.id,
                buyer=buy.maker,
                seller=sell.maker,
                fill_wh=fill,
                exec_price=Decimal(m.exec_price),
                cost=Decimal(m.cost),
                fee=Decimal(m.fee),
                created_at=lifecycle.now(),
                external=False,
            )
            s.add(trade)
            await s.flush()
            s.add(
                Settlement(
                    trade_id=trade.id,
                    market_id=market_id,
                    buy_hash=buy.order_hash,
                    sell_hash=sell.order_hash,
                    status=SettlementStatus.MATCHED,
                    provisional_exec_price=Decimal(m.exec_price),
                )
            )
            buy.reserved_wh = Decimal(lifecycle.reserved(buy) + m.fill_wh)
            sell.reserved_wh = Decimal(lifecycle.reserved(sell) + m.fill_wh)
            await s.flush()
            result.matched += 1
            MATCHES.inc()
            SETTLEMENTS.labels("matched").inc()
            out.order(order_out(buy))
            out.order(order_out(sell))
        if matches:
            log.info("matched", extra={"market": market_id, "pairs": len(matches)})
