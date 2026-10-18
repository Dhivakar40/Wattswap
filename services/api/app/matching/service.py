"""Order acceptance and user-initiated cancellation (the write side of the REST API)."""

import logging
from dataclasses import dataclass
from decimal import Decimal

import redis.asyncio as aioredis
from eth_utils import to_checksum_address
from hexbytes import HexBytes
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.chain import orders as eip712
from app.chain.client import ChainClient
from app.chain.orders import OrderData, OrderFormatError
from app.core.config import Settings
from app.core.errors import AppError
from app.core.metrics import ORDERS
from app.core.ratelimit import hit
from app.matching import lifecycle, snapshot
from app.matching.events import Outbox
from app.matching.funding import Commitment
from app.models import Market, Order, OrderSide, OrderStatus, User
from app.schemas.trading import CancelOut, OrderIn, order_out

log = logging.getLogger("wattswap.orders")

ERC1271_MAGIC = "0x1626ba7e"
ERC1271_ABI: list[dict[str, object]] = [
    {
        "type": "function",
        "name": "isValidSignature",
        "stateMutability": "view",
        "inputs": [{"name": "hash", "type": "bytes32"}, {"name": "signature", "type": "bytes"}],
        "outputs": [{"name": "magicValue", "type": "bytes4"}],
    }
]


def reject(status: int, code: str, message: str, **details: object) -> AppError:
    ORDERS.labels(f"rejected:{code}").inc()
    return AppError(status, code, message, dict(details))


@dataclass(frozen=True)
class Accepted:
    order: Order
    created: bool


async def is_valid_signature(client: ChainClient, o: OrderData, digest: bytes, sig: str) -> bool:
    """Maker authorisation, with the same two routes the contract's SignatureChecker uses:
    ECDSA recovery (EOA) first, then ERC-1271 for makers that have contract code."""
    try:
        if eip712.recover_signer(digest, sig).lower() == o.maker.lower():
            return True
    except OrderFormatError:
        pass
    maker = to_checksum_address(o.maker)
    if not await client.w3.eth.get_code(maker):
        return False
    wallet = client.w3.eth.contract(address=maker, abi=ERC1271_ABI)
    try:
        magic = await wallet.functions.isValidSignature(digest, bytes(HexBytes(sig))).call()
    except Exception:
        return False
    return "0x" + bytes(magic).hex() == ERC1271_MAGIC


def parse_order(body: OrderIn) -> OrderData:
    try:
        return OrderData(
            maker=body.maker.lower(),
            market_id=body.market_id.lower(),
            side=body.side,
            amount_wh=int(body.amount_wh),
            limit_price=int(body.limit_price),
            expiry=int(body.expiry),
            nonce=int(body.nonce),
            salt=body.salt.lower(),
        )
    except OrderFormatError as exc:
        raise reject(422, exc.code, str(exc)) from exc


async def accept_order(
    *,
    settings: Settings,
    session: AsyncSession,
    redis: aioredis.Redis,
    client: ChainClient,
    user: User,
    body: OrderIn,
    out: Outbox,
) -> Accepted:
    """Validate, fund-check and store a signed order. The caller commits and flushes `out`."""
    maker = body.maker.lower()
    if maker != user.address.lower():
        raise reject(403, "maker_mismatch", "the order's maker must be the signed-in address")
    # One placement budget per authenticated address, spent before any expensive work.
    await hit(
        redis, f"orders:{maker}", settings.rate_limit_orders, settings.rate_limit_window_seconds
    )

    data = parse_order(body)
    digest = eip712.order_digest(settings.chain_id, client.exchange_address, data)
    order_hash = "0x" + digest.hex()

    existing = await session.scalar(select(Order).where(Order.order_hash == order_hash))
    if existing is not None:
        return Accepted(existing, False)  # idempotent re-post of the same signed order

    if not await is_valid_signature(client, data, digest, body.signature):
        raise reject(422, "bad_signature", "signature does not match the maker for this order")

    market = await session.scalar(select(Market).where(Market.market_id == data.market_id))
    if market is None or not market.enabled:
        raise reject(422, "market_disabled", "unknown or disabled market")
    if not await client.market_enabled(data.market_id):
        raise reject(422, "market_disabled", "market is not enabled on the exchange")

    if data.amount_wh < settings.min_order_wh:
        raise reject(
            422,
            "order_too_small",
            f"minimum order is {settings.min_order_wh} Wh",
            min_order_wh=settings.min_order_wh,
        )
    if not settings.min_limit_price <= data.limit_price <= settings.max_limit_price:
        raise reject(
            422,
            "bad_price",
            "limit price outside the allowed range",
            min=settings.min_limit_price,
            max=settings.max_limit_price,
        )
    chain_now = await client.latest_block_timestamp()
    if data.expiry < chain_now + settings.min_order_life_seconds + settings.expiry_margin_seconds:
        raise reject(422, "order_expired", "order expires too soon to be settled")
    if data.expiry > chain_now + settings.max_order_life_seconds:
        raise reject(422, "bad_expiry", "order expiry is too far in the future")

    # Serialise acceptance per maker so two concurrent orders cannot both pass the funding check.
    await session.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:k, 0))"), {"k": f"accept:{maker}"}
    )
    open_count = await session.scalar(
        select(func.count())
        .select_from(Order)
        .where(Order.maker == maker, Order.status.in_(lifecycle.ACTIVE))
    )
    if int(open_count or 0) >= settings.max_open_orders_per_maker:
        raise reject(
            429,
            "too_many_open_orders",
            "too many open orders",
            limit=settings.max_open_orders_per_maker,
        )

    active = await snapshot.active_orders_of(session, maker, chain_now)
    candidate = Commitment(
        key="new",
        order_hash=order_hash,
        is_sell=data.side == 1,
        remaining_wh=data.amount_wh,
        limit_price=data.limit_price,
        seq=2**62,
    )
    ev = await snapshot.evaluate(client, maker, active, extra=candidate)
    snap = ev.snapshot
    if snap.cancelled.get(order_hash):
        raise reject(422, "order_cancelled", "this order was already cancelled on-chain")
    if data.nonce < snap.min_nonce:
        raise reject(
            422,
            "nonce_too_low",
            "nonce is below the maker's on-chain nonce floor",
            nonce=data.nonce,
            min_nonce=snap.min_nonce,
        )
    if snap.filled.get(order_hash, 0) > 0:
        raise reject(422, "already_filled", "this order already has fills on-chain")
    for c, why in ev.unfunded:
        if c.key == "new":
            funds = ev.sell_funds if candidate.is_sell else ev.buy_funds
            committed = ev.sell_committed if candidate.is_sell else ev.buy_committed
            token = "WATT (Wh)" if candidate.is_sell else "mUSDC (base units)"
            raise reject(
                422,
                f"insufficient_{why}",
                f"{why} is too low to also cover this order",
                token=token,
                required=candidate.amount,
                already_committed=committed - candidate.amount,
                balance=funds.balance,
                allowance=funds.allowance,
                block=snap.block,
            )

    order = Order(
        order_hash=order_hash,
        maker_id=user.id,
        maker=maker,
        market_id=data.market_id,
        side=OrderSide.SELL if data.side == 1 else OrderSide.BUY,
        amount_wh=Decimal(data.amount_wh),
        limit_price=Decimal(data.limit_price),
        expiry=data.expiry,
        nonce=Decimal(data.nonce),
        salt=data.salt,
        signature=body.signature.lower(),
        status=OrderStatus.OPEN,
        filled_wh=Decimal(0),
        reserved_wh=Decimal(0),
        cancel_pending=False,
    )
    session.add(order)
    await session.flush()
    await session.refresh(order)
    ORDERS.labels("accepted").inc()
    out.order(order_out(order))
    return Accepted(order, True)


async def cancel_by_user(
    *, session: AsyncSession, user: User, order_hash: str, out: Outbox
) -> CancelOut:
    """DELETE /orders/{hash}: SOFT, server-side cancel. Never touches reserved Wh."""
    found = await session.scalar(
        select(Order).where(Order.order_hash == order_hash.lower(), Order.maker == user.address)
    )
    if found is None:
        raise AppError(404, "not_found", "order not found")
    (locked,) = (await lifecycle.lock_orders(session, [found.id])).values()
    if locked.status == OrderStatus.CANCELLED or locked.cancel_pending:
        res = lifecycle.CancelResult(0, lifecycle.reserved(locked), locked.cancel_pending)
    elif locked.status not in lifecycle.ACTIVE:
        raise AppError(
            409,
            "not_cancellable",
            f"order is already {locked.status.value}",
            {"status": locked.status.value},
        )
    else:
        res = await lifecycle.cancel_order(session, locked, "user", "offchain", out)
        ORDERS.labels("cancelled_offchain").inc()
    if res.pending:
        message = (
            f"The unreserved remainder ({res.cancelled_wh} Wh) was cancelled. {res.reserved_wh} Wh "
            "are part of in-flight settlements and will settle or be released; the order is "
            "finalized as cancelled afterwards."
        )
    else:
        message = (
            "Order removed from the book. This is an off-chain cancel: the signature stays valid "
            "on-chain until it expires. Call cancel(order) on the exchange for a binding cancel."
        )
    return CancelOut(
        order=order_out(locked),
        cancelled_wh=str(res.cancelled_wh),
        reserved_wh=str(res.reserved_wh),
        cancel_pending=res.pending,
        message=message,
    )
