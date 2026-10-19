"""Per-user energy and earnings totals for the dashboard (SQL aggregates, not paginated sums)."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Query, Response
from sqlalchemy import ColumnElement, and_, case, func, or_, select
from sqlalchemy.orm import InstrumentedAttribute

from app.auth.deps import SessionDep, UserDep
from app.core.state import RedisDep
from app.matching.events import user_channel
from app.models import Meter, Mint, MintStatus, Reading, Settlement, SettlementStatus, Trade
from app.realtime import streams
from app.realtime.snapshot import set_headers
from app.schemas.trading import PortfolioOut

router = APIRouter(tags=["portfolio"])

Period = Literal["24h", "7d", "30d", "all"]
_WINDOWS = {"24h": timedelta(hours=24), "7d": timedelta(days=7), "30d": timedelta(days=30)}


@router.get("/portfolio", response_model=PortfolioOut)
async def portfolio(
    user: UserDep,
    session: SessionDep,
    redis: RedisDep,
    response: Response,
    period: Annotated[
        Period, Query(description="Window applied to executed_at / confirmed_at")
    ] = "all",
) -> PortfolioOut:
    """Exact totals over ALL of the caller's rows in the window. Definitions: docs/api.md."""
    me = user.address.lower()
    now = datetime.now(UTC)
    since = None if period == "all" else now - _WINDOWS[period]
    set_headers(response, await streams.head(redis, user_channel(me)))  # read before the data

    settled = or_(Settlement.id.is_(None), Settlement.status == SettlementStatus.CONFIRMED)
    when = func.coalesce(Trade.executed_at, Trade.created_at)
    t_where: list[ColumnElement[bool]] = [or_(Trade.buyer == me, Trade.seller == me)]
    if since is not None:
        t_where.append(when >= since)

    def agg(
        side_col: InstrumentedAttribute[str], expr: InstrumentedAttribute[Decimal]
    ) -> ColumnElement[Any]:
        return func.coalesce(func.sum(case((and_(side_col == me, settled), expr), else_=0)), 0)

    trades = (
        await session.execute(
            select(
                agg(Trade.seller, Trade.fill_wh),
                agg(Trade.buyer, Trade.fill_wh),
                agg(Trade.seller, Trade.cost),
                agg(Trade.seller, Trade.fee),
                agg(Trade.buyer, Trade.cost),
                func.count().filter(
                    Settlement.status.in_([SettlementStatus.MATCHED, SettlementStatus.SUBMITTED])
                ),
                func.count().filter(Settlement.status == SettlementStatus.FAILED),
                func.count().filter(settled),
            )
            .select_from(Trade)
            .outerjoin(Settlement, Settlement.trade_id == Trade.id)
            .where(*t_where)
        )
    ).one()
    sold_wh, bought_wh, gross, fees, spent, pending_t, failed_t, confirmed_t = (
        int(x) for x in trades
    )

    m_where: list[ColumnElement[bool]] = [Meter.owner_id == user.id]
    produced_where = [*m_where, Mint.status == MintStatus.CONFIRMED]
    if since is not None:
        produced_where.append(Mint.confirmed_at >= since)
    produced = await session.scalar(
        select(func.coalesce(func.sum(Reading.amount_wh), 0))
        .select_from(Mint)
        .join(Reading, Reading.id == Mint.reading_pk)
        .join(Meter, Meter.id == Reading.meter_pk)
        .where(*produced_where)
    )
    mint_counts = (
        await session.execute(
            select(Mint.status, func.count())
            .join(Reading, Reading.id == Mint.reading_pk)
            .join(Meter, Meter.id == Reading.meter_pk)
            .where(*m_where, *([Mint.created_at >= since] if since is not None else []))
            .group_by(Mint.status)
        )
    ).all()
    by_status = {s: int(n) for s, n in mint_counts}

    return PortfolioOut(
        period=period,
        as_of=now,
        since=since,
        produced_wh=str(int(produced or 0)),
        sold_wh=str(sold_wh),
        bought_wh=str(bought_wh),
        gross_sales_usdc=str(gross),
        fees_usdc=str(fees),
        earned_usdc=str(gross - fees),
        spent_usdc=str(spent),
        confirmed_trades=confirmed_t,
        pending_trades=pending_t,
        failed_trades=failed_t,
        pending_mints=by_status.get(MintStatus.PENDING, 0) + by_status.get(MintStatus.SUBMITTED, 0),
        failed_mints=by_status.get(MintStatus.FAILED, 0),
    )
