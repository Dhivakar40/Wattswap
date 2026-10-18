"""Public market data: markets, latest price + signed quote, tick history, 24 h outlook."""

import json
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy import select

from app.auth.deps import SessionDep
from app.core.errors import AppError
from app.core.ratelimit import ip_limit
from app.core.state import RedisDep
from app.models import Market, PriceTick
from app.pricing import publish, repo
from app.schemas.pricing import MarketOut, RegionOut

router = APIRouter(prefix="/markets", tags=["markets"], dependencies=[Depends(ip_limit("markets"))])


async def market_out(request: Request, m: Market) -> MarketOut:
    onchain: bool | None = None
    chain = request.app.state.chain
    if chain is not None:
        try:
            onchain = bool(await chain.client.exchange.functions.marketEnabled(m.market_id).call())
        except Exception:
            onchain = None
    return MarketOut(
        market_id=m.market_id,
        name=m.name,
        enabled=m.enabled,
        is_primary=m.is_primary,
        region=RegionOut(
            min_lat_e6=m.min_lat_e6,
            max_lat_e6=m.max_lat_e6,
            min_lon_e6=m.min_lon_e6,
            max_lon_e6=m.max_lon_e6,
        ),
        onchain_enabled=onchain,
    )


async def require_market(session: SessionDep, market_id: str) -> Market:
    market = await repo.get_market(session, market_id)
    if market is None:
        raise AppError(404, "not_found", "market not found")
    return market


@router.get("", response_model=list[MarketOut])
async def list_markets(request: Request, session: SessionDep) -> list[MarketOut]:
    await repo.ensure_default_market(session)
    return [await market_out(request, m) for m in await repo.list_markets(session)]


@router.get("/{market_id}/price")
async def latest_price(market_id: str, session: SessionDep, redis: RedisDep) -> dict[str, Any]:
    """The latest tick with its price drivers and the signed quote the relayer uses."""
    market = await require_market(session, market_id)
    raw = await redis.get(publish.latest_key(market.market_id))
    if raw:
        envelope: dict[str, Any] = json.loads(raw)
        return {"seq": envelope["seq"], **envelope["data"]}
    row = await session.scalar(
        select(PriceTick)
        .where(PriceTick.market_id == market.market_id)
        .order_by(PriceTick.ts.desc())
        .limit(1)
    )
    if row is None:
        raise AppError(404, "no_price_yet", "the pricing engine has not produced a tick yet")
    return {"seq": None, **repo.tick_row_json(row), "quote": None}


@router.get("/{market_id}/ticks")
async def ticks(
    market_id: str,
    session: SessionDep,
    limit: Annotated[int, Query(ge=1, le=1000)] = 100,
    before: Annotated[int | None, Query(ge=0, description="unix ts cursor")] = None,
    drivers: Annotated[bool, Query(description="include the full price-drivers breakdown")] = False,
) -> dict[str, Any]:
    from datetime import UTC, datetime

    market = await require_market(session, market_id)
    stmt = (
        select(PriceTick)
        .where(PriceTick.market_id == market.market_id)
        .order_by(PriceTick.ts.desc())
        .limit(limit + 1)
    )
    if before is not None:
        stmt = stmt.where(PriceTick.ts < datetime.fromtimestamp(before, UTC))
    rows = list((await session.scalars(stmt)).all())
    page = rows[:limit]
    return {
        "items": [repo.tick_row_json(r, with_drivers=drivers) for r in page],
        "next_before": int(page[-1].ts.timestamp()) if len(rows) > limit else None,
    }


@router.get("/{market_id}/forecast")
async def forecast(market_id: str, session: SessionDep, redis: RedisDep) -> dict[str, Any]:
    """Hourly supply, demand and implied raw price for the next 24 hours."""
    market = await require_market(session, market_id)
    raw = await redis.get(publish.forecast_key(market.market_id))
    if not raw:
        raise AppError(404, "no_forecast_yet", "no forecast has been computed yet")
    body: dict[str, Any] = json.loads(raw)
    return body
