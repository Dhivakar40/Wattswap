"""Trading REST API: place/cancel/list orders, order book depth, trade tape."""

import json
import logging
import time
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request, Response
from sqlalchemy import or_, select

from app.auth.deps import SessionDep, UserDep
from app.chain.orders import ORDER_TYPES
from app.core.errors import AppError
from app.core.ratelimit import ip_limit
from app.core.state import ChainDep, RedisDep, SettingsDep
from app.matching import events, service
from app.matching.events import Outbox
from app.models import Market, Order, OrderStatus, Settlement, Trade
from app.realtime import streams
from app.realtime.snapshot import SEQ_HEADER
from app.schemas.trading import (
    BookOut,
    CancelOut,
    OrderConfigOut,
    OrderIn,
    OrderOut,
    TradeOut,
    order_out,
    trade_out,
)

log = logging.getLogger("wattswap.orders.api")
router = APIRouter(tags=["trading"], dependencies=[Depends(ip_limit("trading"))])


async def _market(session: SessionDep, market_id: str) -> Market:
    m = await session.scalar(select(Market).where(Market.market_id == market_id.lower()))
    if m is None:
        raise AppError(404, "not_found", "market not found")
    return m


@router.get("/markets/{market_id}/order-config", response_model=OrderConfigOut)
async def order_config(
    market_id: str, session: SessionDep, settings: SettingsDep, chain: ChainDep
) -> OrderConfigOut:
    """Everything a client needs to build, sign and (if it wants a binding cancel) cancel an order."""
    m = await _market(session, market_id)
    client = chain.client
    band: int | None = None
    fee: int | None = None
    try:
        params = await client.exchange_params()
        band, fee = params.band_bps, params.fee_bps
    except Exception as exc:  # chain hiccup: the config is still useful without live parameters
        log.warning("exchange parameters unavailable: %s", exc)
    cancel_abi = [
        i for i in client.exchange_abi if i.get("type") == "function"
        and i.get("name") in ("cancel", "cancelUpTo")
    ]  # fmt: skip
    return OrderConfigOut(
        market_id=m.market_id,
        chain_id=settings.chain_id,
        exchange_address=client.exchange_address,
        watt_address=client.token_address,
        usdc_address=client.usdc_address,
        eip712_domain={
            "name": "WattSwapExchange",
            "version": "1",
            "chainId": settings.chain_id,
            "verifyingContract": client.exchange_address,
        },
        eip712_types=ORDER_TYPES,
        min_order_wh=settings.min_order_wh,
        min_fill_wh=settings.min_fill_wh,
        min_limit_price=settings.min_limit_price,
        max_limit_price=settings.max_limit_price,
        min_order_life_seconds=settings.min_order_life_seconds + settings.expiry_margin_seconds,
        max_order_life_seconds=settings.max_order_life_seconds,
        band_bps=band,
        fee_bps=fee,
        cancel_onchain_abi=cancel_abi,
    )


@router.post("/orders", response_model=OrderOut, status_code=201)
async def place_order(
    body: OrderIn,
    request: Request,
    response: Response,
    user: UserDep,
    session: SessionDep,
    redis: RedisDep,
    settings: SettingsDep,
    chain: ChainDep,
) -> OrderOut:
    out = Outbox()
    accepted = await service.accept_order(
        settings=settings,
        session=session,
        redis=redis,
        client=chain.client,
        user=user,
        body=body,
        out=out,
    )
    await session.commit()
    if accepted.created:
        await out.flush(redis, request.app.state.sessionmaker)
    else:
        response.status_code = 200
    return order_out(accepted.order)


@router.get("/orders", response_model=list[OrderOut])
async def my_orders(
    user: UserDep,
    session: SessionDep,
    redis: RedisDep,
    response: Response,
    status: Annotated[OrderStatus | None, Query()] = None,
    market: Annotated[str | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    before_seq: Annotated[int | None, Query(ge=0)] = None,
) -> list[OrderOut]:
    # Head seq FIRST, then the data: every later change is then a delta with seq > this value.
    response.headers[SEQ_HEADER] = str(
        await streams.head_seq(redis, events.user_channel(user.address))
    )
    q = select(Order).where(Order.maker == user.address.lower())
    if status is not None:
        q = q.where(Order.status == status)
    if market is not None:
        q = q.where(Order.market_id == market.lower())
    if before_seq is not None:
        q = q.where(Order.seq < before_seq)
    rows = await session.scalars(q.order_by(Order.seq.desc()).limit(limit))
    return [order_out(o) for o in rows]


@router.get("/orders/{order_hash}", response_model=OrderOut)
async def get_order(order_hash: str, user: UserDep, session: SessionDep) -> OrderOut:
    o = await session.scalar(
        select(Order).where(
            Order.order_hash == order_hash.lower(), Order.maker == user.address.lower()
        )
    )
    if o is None:
        raise AppError(404, "not_found", "order not found")
    return order_out(o)


@router.delete("/orders/{order_hash}", response_model=CancelOut)
async def cancel_order(
    order_hash: str, request: Request, user: UserDep, session: SessionDep, redis: RedisDep
) -> CancelOut:
    """Soft, server-side cancel. Reserved (in-flight) Wh are never cancelled; see the response."""
    out = Outbox()
    result = await service.cancel_by_user(
        session=session, user=user, order_hash=order_hash, out=out
    )
    await session.commit()
    await out.flush(redis, request.app.state.sessionmaker)
    return result


@router.get("/markets/{market_id}/book", response_model=BookOut)
async def book(
    market_id: str,
    session: SessionDep,
    redis: RedisDep,
    depth: Annotated[int, Query(ge=1, le=100)] = 20,
) -> BookOut:
    """Aggregated price levels (no maker addresses)."""
    m = await _market(session, market_id)
    now = int(time.time())
    head = await streams.head_seq(redis, events.book_channel(m.market_id))  # before the data
    if depth == events.BOOK_DEPTH:
        cached = await redis.get(events.book_key(m.market_id))
        if cached:
            data = BookOut.model_validate(json.loads(cached))
            # Only a cache written for the current head is consistent with the channel's seq.
            if now - data.ts < 5 and data.seq == head:
                return data
    snapshot = await events.book_levels(session, m.market_id, now, depth)
    snapshot.seq = head
    return snapshot


@router.get("/markets/{market_id}/trades", response_model=list[TradeOut])
async def tape(
    market_id: str,
    session: SessionDep,
    redis: RedisDep,
    response: Response,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    before: Annotated[int | None, Query(ge=0, description="unix ts cursor")] = None,
) -> list[TradeOut]:
    """Confirmed trades only."""
    m = await _market(session, market_id)
    response.headers[SEQ_HEADER] = str(
        await streams.head_seq(redis, events.trades_channel(m.market_id))
    )
    q = select(Trade).where(Trade.market_id == m.market_id, Trade.executed_at.is_not(None))
    if before is not None:
        q = q.where(Trade.executed_at < datetime.fromtimestamp(before, UTC))
    rows = await session.scalars(q.order_by(Trade.executed_at.desc()).limit(limit))
    out: list[TradeOut] = []
    for t in rows:
        st = await session.scalar(select(Settlement).where(Settlement.trade_id == t.id))
        out.append(trade_out(t, st))
    return out


@router.get("/trades", response_model=list[TradeOut])
async def my_trades(
    user: UserDep,
    session: SessionDep,
    redis: RedisDep,
    response: Response,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> list[TradeOut]:
    """The caller's trades in every state, with the settlement status and failure reason."""
    me = user.address.lower()
    response.headers[SEQ_HEADER] = str(await streams.head_seq(redis, events.user_channel(me)))
    rows = await session.scalars(
        select(Trade)
        .where(or_(Trade.buyer == me, Trade.seller == me))
        .order_by(Trade.created_at.desc())
        .limit(limit)
    )
    out: list[TradeOut] = []
    for t in rows:
        st = await session.scalar(select(Settlement).where(Settlement.trade_id == t.id))
        out.append(trade_out(t, st))
    return out
