"""Database access for the pricing engine."""

import logging
import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from eth_utils import keccak
from sqlalchemy import case, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    Market,
    Meter,
    Order,
    OrderSide,
    OrderStatus,
    PriceTick,
    PricingEvent,
    PricingParam,
    PricingParamChange,
    Reading,
    RegisterStatus,
    Trade,
    User,
)
from app.pricing.engine import Tick
from app.pricing.params import ParamError, PricingParams, from_mapping
from app.pricing.quotes import SignedQuote
from app.pricing.region import BoundingBox, Cell, cell_of

log = logging.getLogger("wattswap.pricing.repo")

MARKET_NAME = "WATT-mUSDC"
DEFAULT_MARKET_ID = "0x" + keccak(text=MARKET_NAME).hex()
GLOBAL_SCOPE = "*"


def market_id_for(name: str) -> str:
    """The on-chain market id is keccak256(name)."""
    return "0x" + keccak(text=name).hex()


# ------------------------------------------------------------------------------ markets
async def ensure_default_market(session: AsyncSession) -> Market:
    """The one market the contracts know (WATT-mUSDC): global region, primary for the oracle."""
    # INSERT .. ON CONFLICT DO NOTHING: the weather poller and the ticker both call this at start-up
    # (and several processes may), so a check-then-insert would race on a fresh database.
    await session.execute(
        pg_insert(Market)
        .values(
            id=uuid.uuid4(),
            market_id=DEFAULT_MARKET_ID,
            name=MARKET_NAME,
            enabled=True,
            is_primary=True,
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
        )
        .on_conflict_do_nothing(index_elements=["market_id"])
    )
    await session.commit()
    market = await session.scalar(select(Market).where(Market.market_id == DEFAULT_MARKET_ID))
    assert market is not None
    return market


async def list_markets(session: AsyncSession, *, enabled_only: bool = False) -> list[Market]:
    stmt = select(Market).order_by(Market.created_at)
    if enabled_only:
        stmt = stmt.where(Market.enabled.is_(True))
    return list((await session.scalars(stmt)).all())


async def get_market(session: AsyncSession, market_id: str) -> Market | None:
    return await session.scalar(select(Market).where(Market.market_id == market_id.lower()))


def bbox_of(m: Market) -> BoundingBox:
    return BoundingBox(m.min_lat_e6, m.max_lat_e6, m.min_lon_e6, m.max_lon_e6)


@dataclass
class MarketCell:
    cell: Cell
    capacity_w: int = 0
    meters: int = 0
    owners: set[str] = field(default_factory=set)


async def market_cells(session: AsyncSession, market: Market) -> list[MarketCell]:
    """Installed capacity per grid cell, from the registered, confirmed, active meters.

    Re-read every tick, so registering or deactivating a meter changes the next tick's supply.
    """
    rows = await session.execute(
        select(Meter.lat_e6, Meter.lon_e6, Meter.rated_capacity_w, User.address)
        .join(User, User.id == Meter.owner_id)
        .where(Meter.active.is_(True), Meter.register_status == RegisterStatus.CONFIRMED)
    )
    box = bbox_of(market)
    cells: dict[Cell, MarketCell] = {}
    for lat_e6, lon_e6, capacity_w, owner in rows.all():
        if not box.contains(lat_e6, lon_e6):
            continue
        c = cell_of(lat_e6, lon_e6)
        mc = cells.setdefault(c, MarketCell(c))
        mc.capacity_w += int(capacity_w)
        mc.meters += 1
        mc.owners.add(owner)
    return sorted(cells.values(), key=lambda m: m.cell)


async def follow_market_time(
    session: AsyncSession, market: Market, now: datetime, window_s: int
) -> int | None:
    """End of the newest interval ingested for this market while readings are still arriving."""
    box = bbox_of(market)
    rows = await session.execute(
        select(
            func.max(Reading.interval_start + Reading.interval_seconds),
            Meter.lat_e6,
            Meter.lon_e6,
        )
        .join(Meter, Meter.id == Reading.meter_pk)
        .where(Reading.received_at >= now - timedelta(seconds=window_s))
        .group_by(Meter.lat_e6, Meter.lon_e6)
    )
    ends = [int(end) for end, lat, lon in rows.all() if end is not None and box.contains(lat, lon)]
    return max(ends) if ends else None


# -------------------------------------------------------------------- order book inputs
async def open_order_wh(session: AsyncSession, market_id: str, side: OrderSide, now_ts: int) -> int:
    """Unfilled Wh of open, unexpired orders on one side of the book.

    A cancel-pending order only counts its in-flight part: the rest has already left the book."""
    open_wh = case(
        (Order.cancel_pending.is_(True), Order.reserved_wh), else_=Order.amount_wh - Order.filled_wh
    )
    total = await session.scalar(
        select(func.coalesce(func.sum(open_wh), 0)).where(
            Order.market_id == market_id,
            Order.side == side,
            Order.status.in_([OrderStatus.OPEN, OrderStatus.PARTIALLY_FILLED]),
            Order.expiry > now_ts,
        )
    )
    return int(total or 0)


async def recent_fill_wh(session: AsyncSession, market_id: str, since: datetime) -> int:
    """Wh actually traded in this market since `since`: only fills that are CONFIRMED on-chain
    (a matched-then-failed trade never moved any energy, so it is not fill velocity)."""
    total = await session.scalar(
        select(func.coalesce(func.sum(Trade.fill_wh), 0)).where(
            Trade.market_id == market_id,
            Trade.executed_at.is_not(None),
            Trade.executed_at >= since,
        )
    )
    return int(total or 0)


async def stored_overrides(session: AsyncSession, market_id: str) -> dict[str, Any]:
    """Global overrides, then this market's, later ones winning."""
    rows = await session.scalars(
        select(PricingParam).where(PricingParam.scope.in_([GLOBAL_SCOPE, market_id]))
    )
    merged: dict[str, Any] = {}
    by_scope: dict[str, dict[str, Any]] = defaultdict(dict)
    for row in rows:
        by_scope[row.scope][row.key] = row.value
    merged.update(by_scope.get(GLOBAL_SCOPE, {}))
    merged.update(by_scope.get(market_id, {}))
    return merged


async def effective_params(
    session: AsyncSession, market_id: str, tick_seconds: int
) -> PricingParams:
    base = PricingParams(tick_seconds=tick_seconds)
    overrides = await stored_overrides(session, market_id)
    try:
        return from_mapping(overrides, base=base)
    except ParamError as exc:
        log.error("stored pricing parameters are invalid (%s); using defaults", exc)
        return base


async def set_overrides(
    session: AsyncSession,
    scope: str,
    values: dict[str, Any],
    changed_by: str,
    now: datetime,
) -> None:
    """Persist overrides and append an audit row per changed key. Caller validates first."""
    existing = {
        r.key: r
        for r in await session.scalars(select(PricingParam).where(PricingParam.scope == scope))
    }
    for key, value in values.items():
        row = existing.get(key)
        old = None if row is None else row.value
        if row is not None and old == value:
            continue
        if row is None:
            session.add(
                PricingParam(
                    scope=scope, key=key, value=value, updated_by=changed_by, updated_at=now
                )
            )
        else:
            row.value, row.updated_by, row.updated_at = value, changed_by, now
        session.add(
            PricingParamChange(
                scope=scope,
                key=key,
                old_value=old,
                new_value=value,
                changed_by=changed_by,
                changed_at=now,
            )
        )
    await session.commit()


async def clear_override(
    session: AsyncSession, scope: str, key: str, changed_by: str, now: datetime
) -> bool:
    row = await session.scalar(
        select(PricingParam).where(PricingParam.scope == scope, PricingParam.key == key)
    )
    if row is None:
        return False
    session.add(
        PricingParamChange(
            scope=scope,
            key=key,
            old_value=row.value,
            new_value=None,
            changed_by=changed_by,
            changed_at=now,
        )
    )
    await session.delete(row)
    await session.commit()
    return True


# ----------------------------------------------------------------------------- ticks
def _dt(ts: int) -> datetime:
    return datetime.fromtimestamp(ts, UTC)


async def save_tick(
    session: AsyncSession,
    market_id: str,
    tick: Tick,
    quote: SignedQuote | None,
    clock_mode: str,
) -> PriceTick:
    ages = [w["age_s"] for w in tick.drivers["weather"]]
    row = PriceTick(
        ts=_dt(tick.ts),
        market_id=market_id,
        raw_price=Decimal(tick.raw_price),
        price=Decimal(tick.price),
        ema_price=tick.ema_price,
        supply_w=Decimal(tick.supply_w),
        demand_w=Decimal(tick.demand_w),
        band_bps=tick.band_bps,
        breaker_state=tick.breaker.mode.value,
        breaker_reason=tick.breaker.reason,
        stale=tick.weather_stale,
        weather_age_s=max(ages) if ages else None,
        vol_bps=tick.vol_bps,
        market_time=tick.market_time,
        quote_timestamp=None if quote is None else quote.quote.timestamp,
        clock_mode=clock_mode,
        signature=None if quote is None else quote.signature,
        drivers=tick.drivers,
    )
    session.add(row)
    for event in tick.events:
        session.add(
            PricingEvent(
                ts=_dt(tick.ts), market_id=market_id, kind=str(event["kind"]), details=event
            )
        )
    await session.commit()
    return row


async def recent_ticks(session: AsyncSession, market_id: str, limit: int) -> list[dict[str, Any]]:
    """The last `limit` ticks, oldest first, in the shape `engine.restore_state` expects."""
    rows = (
        await session.scalars(
            select(PriceTick)
            .where(PriceTick.market_id == market_id)
            .order_by(PriceTick.ts.desc())
            .limit(limit)
        )
    ).all()
    return [
        {
            "ts": int(r.ts.timestamp()),
            "price": int(r.price),
            "raw_price": int(r.raw_price),
            "drivers": r.drivers,
        }
        for r in reversed(rows)
    ]


def tick_row_json(r: PriceTick, *, with_drivers: bool = True) -> dict[str, Any]:
    out: dict[str, Any] = {
        "ts": int(r.ts.timestamp()),
        "marketId": r.market_id,
        "price": int(r.price),
        "rawPrice": int(r.raw_price),
        "emaPrice": str(r.ema_price),
        "supplyW": int(r.supply_w),
        "demandW": int(r.demand_w),
        "bandBps": r.band_bps,
        "breaker": r.breaker_state,
        "breakerReason": r.breaker_reason,
        "weatherStale": r.stale,
        "weatherAgeS": r.weather_age_s,
        "volBps": None if r.vol_bps is None else str(r.vol_bps),
        "marketTime": r.market_time,
        "quoteTimestamp": r.quote_timestamp,
        "clockMode": r.clock_mode,
        "signature": r.signature,
    }
    if with_drivers:
        out["drivers"] = r.drivers
    return out
