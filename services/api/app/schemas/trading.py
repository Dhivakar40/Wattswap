"""Request/response models for the trading API. uint256 values travel as decimal strings."""

from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from app.models import Order, OrderSide, OrderStatus, Settlement, SettlementStatus, Trade

Hex32 = Annotated[str, StringConstraints(pattern=r"^0x[0-9a-fA-F]{64}$")]
HexAddress = Annotated[str, StringConstraints(pattern=r"^0x[0-9a-fA-F]{40}$")]
Uint = Annotated[str, StringConstraints(pattern=r"^[0-9]{1,78}$")]


class OrderIn(BaseModel):
    """A signed `Order` exactly as in the EIP-712 struct, plus the signature."""

    maker: HexAddress
    market_id: Hex32 = Field(alias="marketId")
    side: Literal[0, 1] = Field(description="0 = buy, 1 = sell")
    amount_wh: Uint = Field(alias="amountWh")
    limit_price: Uint = Field(alias="limitPrice", description="mUSDC base units (6 dp) per kWh")
    expiry: Uint
    nonce: Uint
    salt: Hex32
    signature: Annotated[str, StringConstraints(pattern=r"^0x[0-9a-fA-F]+$", max_length=200)]

    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class OrderOut(BaseModel):
    order_hash: str
    maker: str
    market_id: str
    side: Literal["buy", "sell"]
    amount_wh: str
    limit_price: str
    expiry: int
    nonce: str
    salt: str
    status: Literal["open", "partially_filled", "filled", "cancelled", "expired"]
    filled_wh: str
    reserved_wh: str
    remaining_wh: str  # amount - filled
    cancel_reason: str | None
    cancel_type: Literal["offchain", "onchain"] | None
    cancel_pending: bool
    created_at: datetime
    updated_at: datetime


class CancelOut(BaseModel):
    """Result of DELETE /orders/{hash}. The state is always spelled out."""

    order: OrderOut
    cancel_type: Literal["offchain"] = "offchain"
    cancelled_wh: str = Field(
        description="Wh removed from the book right now (the unreserved part)"
    )
    reserved_wh: str = Field(description="Wh still reserved by in-flight settlements")
    cancel_pending: bool = Field(
        description="True while reserved Wh settle; the order is finalized as cancelled afterwards"
    )
    binding_onchain: Literal[False] = Field(
        default=False,
        description="An off-chain cancel does not void the signature on-chain; use cancel/cancelUpTo",
    )
    message: str


class TradeOut(BaseModel):
    id: str
    market_id: str
    buyer: str
    seller: str
    fill_wh: str
    exec_price: str
    cost: str
    fee: str
    status: Literal["matched", "submitted", "confirmed", "failed"]
    failure_reason: str | None
    tx_hash: str | None
    block_number: int | None
    executed_at: datetime | None
    created_at: datetime
    external: bool


class BookLevel(BaseModel):
    price: str
    amount_wh: str
    orders: int


class BookOut(BaseModel):
    market_id: str
    bids: list[BookLevel]
    asks: list[BookLevel]
    ts: int


class OrderConfigOut(BaseModel):
    market_id: str
    chain_id: int
    exchange_address: str
    watt_address: str
    usdc_address: str
    eip712_domain: dict[str, str | int]
    eip712_types: dict[str, list[dict[str, str]]]
    primary_type: Literal["Order"] = "Order"
    min_order_wh: int
    min_fill_wh: int
    min_limit_price: int
    max_limit_price: int
    min_order_life_seconds: int
    max_order_life_seconds: int
    band_bps: int | None
    fee_bps: int | None
    cancel_onchain_abi: list[dict[str, object]] = Field(
        description="ABI fragments of cancel(order) and cancelUpTo(nonce): the binding cancels"
    )


def order_out(o: Order) -> OrderOut:
    return OrderOut(
        order_hash=o.order_hash,
        maker=o.maker,
        market_id=o.market_id,
        side="buy" if o.side == OrderSide.BUY else "sell",
        amount_wh=str(int(o.amount_wh)),
        limit_price=str(int(o.limit_price)),
        expiry=int(o.expiry),
        nonce=str(int(o.nonce)),
        salt=o.salt,
        status=OrderStatus(o.status).value,
        filled_wh=str(int(o.filled_wh)),
        reserved_wh=str(int(o.reserved_wh)),
        remaining_wh=str(int(o.amount_wh) - int(o.filled_wh)),
        cancel_reason=o.cancel_reason,
        cancel_type=o.cancel_type,
        cancel_pending=bool(o.cancel_pending),
        created_at=o.created_at,
        updated_at=o.updated_at,
    )


def trade_out(t: Trade, s: Settlement | None) -> TradeOut:
    status = "confirmed" if s is None else SettlementStatus(s.status).value
    return TradeOut(
        id=str(t.id),
        market_id=t.market_id,
        buyer=t.buyer,
        seller=t.seller,
        fill_wh=str(int(t.fill_wh)),
        exec_price=str(int(t.exec_price)),
        cost=str(int(t.cost)),
        fee=str(int(t.fee)),
        status=status,
        failure_reason=None if s is None else s.failure_reason,
        tx_hash=t.tx_hash if t.tx_hash else (None if s is None else s.tx_hash),
        block_number=t.block_number,
        executed_at=t.executed_at,
        created_at=t.created_at,
        external=t.external,
    )
