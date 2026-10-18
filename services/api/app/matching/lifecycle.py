"""Every order/settlement state transition that touches Wh reservations, in one place.

Invariants these functions maintain (the DB CHECK `filled_wh + reserved_wh <= amount_wh` is the
backstop):
  * `reserved_wh` of an order = sum of `fill_wh` over its `matched`/`submitted` settlements.
  * Releasing a settlement (failed) gives the Wh back to BOTH orders.
  * A soft cancel (`cancel_pending`) never touches reserved Wh; the order is finalized when its
    last in-flight settlement resolves.
Callers run these inside a transaction and flush the `Outbox` after commit.
"""

import logging
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.metrics import SETTLE_FAILURES, SETTLEMENTS
from app.matching.events import Outbox
from app.models import Order, OrderStatus, PairCooldown, Settlement, SettlementStatus, Trade
from app.schemas.trading import order_out, trade_out

log = logging.getLogger("wattswap.lifecycle")

ACTIVE = (OrderStatus.OPEN, OrderStatus.PARTIALLY_FILLED)
IN_FLIGHT = (SettlementStatus.MATCHED, SettlementStatus.SUBMITTED)


def amount(o: Order) -> int:
    return int(o.amount_wh)


def filled(o: Order) -> int:
    return int(o.filled_wh)


def reserved(o: Order) -> int:
    return int(o.reserved_wh)


def matchable(o: Order) -> int:
    """Wh the matcher may still allocate. Zero for a cancel-pending order."""
    if o.cancel_pending or o.status not in ACTIVE:
        return 0
    return max(0, amount(o) - filled(o) - reserved(o))


def now() -> datetime:
    return datetime.now(UTC)


async def lock_orders(session: AsyncSession, ids: Iterable[UUID]) -> dict[UUID, Order]:
    """SELECT ... FOR UPDATE in a deterministic order (id), which avoids lock-order deadlocks."""
    wanted = sorted(set(ids))
    if not wanted:
        return {}
    rows = await session.scalars(
        select(Order)
        .where(Order.id.in_(wanted))
        .order_by(Order.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    return {o.id: o for o in rows}


async def settlements_of_order(
    session: AsyncSession, order_hash: str, statuses: tuple[SettlementStatus, ...] = IN_FLIGHT
) -> list[Settlement]:
    rows = await session.scalars(
        select(Settlement)
        .where(
            or_(Settlement.buy_hash == order_hash, Settlement.sell_hash == order_hash),
            Settlement.status.in_(statuses),
        )
        .order_by(Settlement.created_at)
    )
    return list(rows)


def _set_status_from_fills(o: Order) -> None:
    if o.status in ACTIVE:
        if filled(o) >= amount(o):
            o.status = OrderStatus.FILLED
        elif filled(o) > 0:
            o.status = OrderStatus.PARTIALLY_FILLED
        else:
            o.status = OrderStatus.OPEN


def finalize_pending(o: Order) -> None:
    """Complete a soft cancel once nothing is reserved any more."""
    if o.cancel_pending and reserved(o) == 0 and o.status in ACTIVE:
        o.status = OrderStatus.FILLED if filled(o) >= amount(o) else OrderStatus.CANCELLED
        o.cancel_pending = False


def apply_fill(o: Order, fill: int, *, from_reserved: bool) -> None:
    """A fill became final on-chain."""
    if from_reserved:
        o.reserved_wh = Decimal(max(0, reserved(o) - fill))
    o.filled_wh = Decimal(filled(o) + fill)
    _set_status_from_fills(o)
    finalize_pending(o)


def unapply_fill(o: Order, fill: int, *, to_reserved: bool) -> None:
    """Reverse `apply_fill` (a reorg removed the TradeSettled)."""
    o.filled_wh = Decimal(max(0, filled(o) - fill))
    if to_reserved:
        o.reserved_wh = Decimal(reserved(o) + fill)
    if o.status == OrderStatus.CANCELLED and to_reserved and o.cancel_type is not None:
        o.status = OrderStatus.OPEN  # the soft cancel it finalized is pending again
        o.cancel_pending = True
    elif o.status == OrderStatus.FILLED:
        o.status = OrderStatus.OPEN
    _set_status_from_fills(o)


# ------------------------------------------------------------------------------ settlements
async def fail_settlement(session: AsyncSession, st: Settlement, reason: str, out: Outbox) -> bool:
    """matched|submitted -> failed. Returns the Wh to both orders. Idempotent."""
    if st.status not in IN_FLIGHT:
        return False
    trade = await session.get(Trade, st.trade_id)
    if trade is None:
        raise RuntimeError(f"settlement {st.id} has no trade")
    fill = int(trade.fill_wh)
    orders = await lock_orders(session, [trade.buy_order_id, trade.sell_order_id])
    for o in orders.values():
        o.reserved_wh = Decimal(max(0, reserved(o) - fill))
    st.status = SettlementStatus.FAILED
    st.failure_reason = reason
    SETTLEMENTS.labels("failed").inc()
    SETTLE_FAILURES.labels(reason.split(":")[0]).inc()
    await session.flush()
    for o in orders.values():
        finalize_pending(o)
        out.order(order_out(o))
    out.trade(trade_out(trade, st))
    log.info("settlement failed", extra={"settlement": str(st.id), "reason": reason})
    return True


async def make_room(session: AsyncSession, o: Order, extra_filled: int, out: Outbox) -> None:
    """Free reserved Wh (failing the newest in-flight settlements) so that
    `filled + extra + reserved <= amount` holds. Needed when the chain shows fills we had
    reserved for something else (external settles, reorg re-ordering)."""
    while filled(o) + extra_filled + reserved(o) > amount(o) and reserved(o) > 0:
        in_flight = await settlements_of_order(session, o.order_hash)
        if not in_flight:
            o.reserved_wh = Decimal(0)
            break
        newest = in_flight[-1]
        await fail_settlement(session, newest, "overfill:chain_ahead_of_db", out)
        await session.refresh(o)


# ------------------------------------------------------------------------------ cancellation
@dataclass(frozen=True)
class CancelResult:
    cancelled_wh: int  # removed from the book right now
    reserved_wh: int  # still in flight
    pending: bool


async def cancel_order(
    session: AsyncSession,
    o: Order,
    reason: str,
    cancel_type: str,
    out: Outbox,
    *,
    fail_inflight: bool = False,
) -> CancelResult:
    """Cancel an order.

    Default (soft) mode never touches the reserved part: the unreserved remainder leaves the book
    now; with Wh reserved the order stays `cancel_pending` and is finalized when the in-flight
    settlements resolve. With `fail_inflight` (the order is known to be unusable: cancelled
    on-chain, unfunded, ...) the in-flight settlements are failed first, so nothing stays reserved.
    """
    if cancel_type not in ("offchain", "onchain"):
        raise ValueError(cancel_type)
    if o.status not in ACTIVE:
        # Already cancelled off-chain, and now also cancelled on-chain: record the stronger fact.
        if o.status == OrderStatus.CANCELLED and cancel_type == "onchain":
            o.cancel_type = "onchain"
            out.order(order_out(o))
        return CancelResult(0, reserved(o), False)

    if fail_inflight:
        for st in await settlements_of_order(session, o.order_hash):
            trade = await session.get(Trade, st.trade_id)
            if trade is not None:
                await lock_orders(session, [o.id, trade.buy_order_id, trade.sell_order_id])
            prefix = "cancel_race" if cancel_type == "onchain" else "order_cancelled"
            await fail_settlement(session, st, f"{prefix}:{reason}", out)
        await session.refresh(o)

    unreserved = max(0, amount(o) - filled(o) - reserved(o))
    o.cancel_reason = reason
    o.cancel_type = cancel_type
    if reserved(o) > 0:
        o.cancel_pending = True
        result = CancelResult(unreserved, reserved(o), True)
    else:
        o.status = OrderStatus.CANCELLED
        o.cancel_pending = False
        result = CancelResult(unreserved, 0, False)
    await session.flush()
    out.order(order_out(o))
    return result


# ------------------------------------------------------------------------ pair cooldowns
async def cooled_pairs(session: AsyncSession) -> set[tuple[str, str]]:
    rows = await session.execute(
        select(PairCooldown.buy_hash, PairCooldown.sell_hash).where(PairCooldown.until > now())
    )
    return {(b, s) for b, s in rows.all()}


async def record_release(
    session: AsyncSession, buy_hash: str, sell_hash: str, reason: str, base_s: int, cap_s: int
) -> int:
    """Put a released pair on an exponentially growing cooldown; returns the cooldown seconds."""
    row = await session.get(PairCooldown, (buy_hash, sell_hash))
    failures = 1 if row is None else row.failures + 1
    delay = min(cap_s, base_s * (1 << min(failures - 1, 30)))
    until = now() + timedelta(seconds=delay)
    if row is None:
        session.add(
            PairCooldown(
                buy_hash=buy_hash,
                sell_hash=sell_hash,
                failures=failures,
                until=until,
                last_reason=reason,
            )
        )
    else:
        row.failures = failures
        row.until = until
        row.last_reason = reason
    await session.flush()
    return delay


async def clear_cooldown(session: AsyncSession, buy_hash: str, sell_hash: str) -> None:
    row = await session.get(PairCooldown, (buy_hash, sell_hash))
    if row is not None:
        await session.delete(row)
