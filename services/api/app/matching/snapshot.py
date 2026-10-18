"""Funding evaluation for a maker: DB orders + a one-block chain snapshot -> who is over-committed.

The snapshot pins ONE block for balances, allowances and every `exchange.filled(hash)`. Commitments
use `amount - filled_on_chain_at_that_block`, not the DB's `filled_wh`: between a settle being mined
and the listener confirming it the balance has already dropped while the DB has not yet counted the
fill, and mixing the two would cancel healthy orders. Reserved Wh of in-flight settlements are part
of "remaining" until the fill is final on-chain.
"""

import logging
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.chain.client import ChainClient, MakerSnapshot
from app.core.metrics import FUNDING_CANCELS
from app.matching import lifecycle
from app.matching.events import Outbox
from app.matching.funding import Commitment, Funds, orders_to_cancel, total_commitment
from app.models import Order, OrderSide

log = logging.getLogger("wattswap.funding")


@dataclass
class FundingEvaluation:
    snapshot: MakerSnapshot
    sell_funds: Funds
    buy_funds: Funds
    sell_committed: int
    buy_committed: int
    unfunded: list[tuple[Commitment, str]] = field(
        default_factory=list
    )  # (order, balance|allowance)
    voided_onchain: list[tuple[Commitment, str]] = field(default_factory=list)  # (order, reason)


def _commitment(o: Order, chain_filled: int) -> Commitment:
    remaining = max(0, lifecycle.amount(o) - chain_filled)
    if o.cancel_pending:  # only the in-flight part is still owed
        remaining = min(remaining, lifecycle.reserved(o))
    return Commitment(
        key=str(o.id),
        order_hash=o.order_hash,
        is_sell=o.side == OrderSide.SELL,
        remaining_wh=remaining,
        limit_price=int(o.limit_price),
        seq=o.seq,
    )


async def active_orders_of(session: AsyncSession, maker: str, chain_now: int) -> list[Order]:
    rows = await session.scalars(
        select(Order)
        .where(
            Order.maker == maker.lower(),
            Order.status.in_(lifecycle.ACTIVE),
            Order.expiry > chain_now,
        )
        .order_by(Order.seq)
    )
    return list(rows)


async def evaluate(
    client: ChainClient,
    maker: str,
    orders: list[Order],
    *,
    extra: Commitment | None = None,
) -> FundingEvaluation:
    """Funding verdict for `orders` (+ `extra`, a not-yet-stored order) at one chain block."""
    hashes = [o.order_hash for o in orders]
    snap = await client.maker_snapshot(maker, hashes)
    sells: list[Commitment] = []
    buys: list[Commitment] = []
    voided: list[tuple[Commitment, str]] = []
    for o in orders:
        c = _commitment(o, snap.filled.get(o.order_hash, 0))
        if snap.cancelled.get(o.order_hash):
            voided.append((c, "on_chain_cancel"))
        elif int(o.nonce) < snap.min_nonce:
            voided.append((c, "nonce_floor"))
        elif c.remaining_wh > 0:
            (sells if c.is_sell else buys).append(c)
    if extra is not None:
        (sells if extra.is_sell else buys).append(extra)
    sell_funds = Funds(snap.watt_balance, snap.watt_allowance)
    buy_funds = Funds(snap.usdc_balance, snap.usdc_allowance)
    return FundingEvaluation(
        snapshot=snap,
        sell_funds=sell_funds,
        buy_funds=buy_funds,
        sell_committed=total_commitment(sells),
        buy_committed=total_commitment(buys),
        unfunded=orders_to_cancel(sells, sell_funds) + orders_to_cancel(buys, buy_funds),
        voided_onchain=voided,
    )


async def enforce(
    session: AsyncSession,
    client: ChainClient,
    maker: str,
    out: Outbox,
    chain_now: int,
) -> tuple[set[str], FundingEvaluation | None]:
    """Cancel the maker's voided/unfunded orders (newest first).

    Returns (cancelled hashes, the evaluation it acted on)."""
    orders = await active_orders_of(session, maker, chain_now)
    if not orders:
        return set(), None
    ev = await evaluate(client, maker, orders)
    by_key = {str(o.id): o for o in orders}
    cancelled: set[str] = set()
    for c, reason in ev.voided_onchain:
        o = by_key[c.key]
        await lifecycle.lock_orders(session, [o.id])
        await lifecycle.cancel_order(session, o, reason, "onchain", out, fail_inflight=True)
        cancelled.add(o.order_hash)
    for c, reason in ev.unfunded:
        o = by_key[c.key]
        await lifecycle.lock_orders(session, [o.id])
        await lifecycle.cancel_order(
            session, o, f"unfunded:{reason}", "offchain", out, fail_inflight=True
        )
        FUNDING_CANCELS.labels(reason).inc()
        cancelled.add(o.order_hash)
        log.warning(
            "order auto-cancelled (unfunded)",
            extra={"order": o.order_hash, "maker": maker, "reason": reason},
        )
    return cancelled, ev
