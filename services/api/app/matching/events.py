"""Redis publishing for the book, trades and per-user streams (consumed by the Phase 5 gateway).

Envelope (same as the price channel): {type, channel, seq, ts, data}. `seq` is a per-channel Redis
counter so a client can detect gaps and re-snapshot over REST. Events are collected in an `Outbox`
during a DB transaction and flushed only AFTER it commits, so a rolled-back change is never
announced.
"""

import json
import time
from typing import Any

import redis.asyncio as aioredis
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models import Order, OrderSide, OrderStatus
from app.schemas.trading import BookLevel, BookOut, OrderOut, TradeOut

BOOK_DEPTH = 20
ACTIVE = (OrderStatus.OPEN, OrderStatus.PARTIALLY_FILLED)


def book_channel(market_id: str) -> str:
    return f"orderbook:{market_id}"


def trades_channel(market_id: str) -> str:
    return f"trades:{market_id}"


def user_channel(address: str) -> str:
    return f"user:{address.lower()}"


def book_key(market_id: str) -> str:
    return f"book:{market_id}"


async def publish(redis: aioredis.Redis, channel: str, type_: str, data: dict[str, Any]) -> None:
    seq = int(await redis.incr(f"{channel}:seq"))
    envelope = {"type": type_, "channel": channel, "seq": seq, "ts": int(time.time()), "data": data}
    await redis.publish(channel, json.dumps(envelope, separators=(",", ":")))


async def book_levels(
    session: AsyncSession, market_id: str, now_ts: int, depth: int = BOOK_DEPTH
) -> BookOut:
    """Aggregated depth: matchable (unreserved, not cancel-pending, unexpired) Wh per price."""

    async def side(s: OrderSide) -> list[BookLevel]:
        matchable = Order.amount_wh - Order.filled_wh - Order.reserved_wh
        price_order = Order.limit_price.desc() if s == OrderSide.BUY else Order.limit_price.asc()
        rows = await session.execute(
            select(Order.limit_price, func.sum(matchable), func.count())
            .where(
                Order.market_id == market_id,
                Order.side == s,
                Order.status.in_(ACTIVE),
                Order.cancel_pending.is_(False),
                Order.expiry > now_ts,
            )
            .group_by(Order.limit_price)
            .having(func.sum(matchable) > 0)
            .order_by(price_order)
            .limit(depth)
        )
        return [
            BookLevel(price=str(int(p)), amount_wh=str(int(w)), orders=int(n))
            for p, w, n in rows.all()
        ]

    return BookOut(
        market_id=market_id,
        bids=await side(OrderSide.BUY),
        asks=await side(OrderSide.SELL),
        ts=now_ts,
    )


class Outbox:
    """Collects events during a transaction; `flush` publishes them after commit."""

    def __init__(self) -> None:
        self._user: list[tuple[str, str, dict[str, Any]]] = []
        self._trades: list[tuple[str, dict[str, Any]]] = []
        self._markets: set[str] = set()

    def order(self, order: OrderOut) -> None:
        self._user.append((order.maker, "order", order.model_dump(mode="json")))
        self._markets.add(order.market_id)

    def trade(self, trade: TradeOut) -> None:
        payload = trade.model_dump(mode="json")
        self._user.append((trade.buyer, "fill", payload))
        self._user.append((trade.seller, "fill", payload))
        self._trades.append((trade.market_id, payload))
        self._markets.add(trade.market_id)

    def book_changed(self, market_id: str) -> None:
        self._markets.add(market_id)

    def __bool__(self) -> bool:
        return bool(self._user or self._trades or self._markets)

    async def flush(
        self, redis: aioredis.Redis, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        now = int(time.time())
        for address, type_, data in self._user:
            await publish(redis, user_channel(address), type_, data)
        for market_id, data in self._trades:
            await publish(redis, trades_channel(market_id), "trade", data)
        for market_id in self._markets:
            async with sessionmaker() as s:
                book = await book_levels(s, market_id, now)
            body = book.model_dump(mode="json")
            await redis.set(book_key(market_id), json.dumps(body, separators=(",", ":")), ex=3600)
            await publish(redis, book_channel(market_id), "orderbook", body)
        self._user.clear()
        self._trades.clear()
        self._markets.clear()
