"""ORM tables."""

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Identity,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy import Enum as SAEnum
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import (
    Address,
    Base,
    Bytes32,
    MintStatus,
    OrderSide,
    OrderStatus,
    RegisterStatus,
    SettlementStatus,
    TimestampMixin,
    Uint256,
    UserRole,
    uuid_pk,
)


def _enum[E: type](cls: E, name: str) -> SAEnum:
    return SAEnum(
        cls,
        name=name,
        values_callable=lambda e: [m.value for m in e],
    )


class User(Base, TimestampMixin):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = uuid_pk()
    address: Mapped[str] = mapped_column(Address, unique=True, nullable=False)  # lowercase
    role: Mapped[UserRole] = mapped_column(
        _enum(UserRole, "user_role"), default=UserRole.USER, nullable=False
    )
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    meters: Mapped[list["Meter"]] = relationship(back_populates="owner")


class Meter(Base, TimestampMixin):
    __tablename__ = "meters"
    __table_args__ = (
        CheckConstraint("rated_capacity_w > 0", name="capacity_positive"),
        CheckConstraint("lat_e6 BETWEEN -90000000 AND 90000000", name="lat_range"),
        CheckConstraint("lon_e6 BETWEEN -180000000 AND 180000000", name="lon_range"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    meter_id: Mapped[str] = mapped_column(Bytes32, unique=True, nullable=False)  # bytes32 hex
    owner_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=False, index=True
    )
    device_signer: Mapped[str] = mapped_column(Address, nullable=False)  # lowercase
    label: Mapped[str] = mapped_column(String(80), nullable=False)
    rated_capacity_w: Mapped[int] = mapped_column(Integer, nullable=False)
    lat_e6: Mapped[int] = mapped_column(Integer, nullable=False)
    lon_e6: Mapped[int] = mapped_column(Integer, nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    # Every meter is simulated in v1. The column exists so real hardware can flip it later.
    is_simulated: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    register_status: Mapped[RegisterStatus] = mapped_column(
        _enum(RegisterStatus, "register_status"), default=RegisterStatus.PENDING, nullable=False
    )
    register_tx_hash: Mapped[str | None] = mapped_column(Bytes32)
    register_error: Mapped[str | None] = mapped_column(Text)

    owner: Mapped[User] = relationship(back_populates="meters")
    readings: Mapped[list["Reading"]] = relationship(back_populates="meter")


class Reading(Base):
    __tablename__ = "readings"
    __table_args__ = (
        UniqueConstraint("meter_pk", "interval_start", name="uq_readings_meter_interval"),
        CheckConstraint("amount_wh > 0", name="amount_positive"),
        CheckConstraint("interval_seconds > 0", name="interval_positive"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    reading_id: Mapped[str] = mapped_column(Bytes32, unique=True, nullable=False)
    meter_pk: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("meters.id"), nullable=False, index=True
    )
    amount_wh: Mapped[int] = mapped_column(BigInteger, nullable=False)
    interval_start: Mapped[int] = mapped_column(BigInteger, nullable=False)  # unix seconds
    interval_seconds: Mapped[int] = mapped_column(Integer, nullable=False)
    signature: Mapped[str] = mapped_column(String(132), nullable=False)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    meter: Mapped[Meter] = relationship(back_populates="readings")
    mint: Mapped["Mint | None"] = relationship(back_populates="reading", uselist=False)


class Mint(Base, TimestampMixin):
    __tablename__ = "mints"
    __table_args__ = (Index("ix_mints_status", "status"),)

    id: Mapped[uuid.UUID] = uuid_pk()
    reading_pk: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("readings.id"), unique=True, nullable=False
    )
    status: Mapped[MintStatus] = mapped_column(
        _enum(MintStatus, "mint_status"), default=MintStatus.PENDING, nullable=False
    )
    tx_hash: Mapped[str | None] = mapped_column(Bytes32)
    # Signed raw transaction, persisted before broadcast so a crash can re-send the same tx.
    raw_tx: Mapped[str | None] = mapped_column(Text)
    tx_nonce: Mapped[int | None] = mapped_column(BigInteger)
    block_number: Mapped[int | None] = mapped_column(BigInteger)
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    failure_reason: Mapped[str | None] = mapped_column(Text)
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    reading: Mapped[Reading] = relationship(back_populates="mint")


class Order(Base, TimestampMixin):
    """A maker's signed EIP-712 order. `order_hash` is the exchange's `hashOrder` digest.

    Wh accounting: `filled_wh` is confirmed on-chain fills only; `reserved_wh` is in `matched` or
    `submitted` settlements. Matchable remainder = amount - filled - reserved. The CHECK below is
    the backstop that makes over-reservation impossible even with two writers.
    """

    __tablename__ = "orders"

    id: Mapped[uuid.UUID] = uuid_pk()
    # Strict time priority: a database sequence, immune to clock ties and skew.
    seq: Mapped[int] = mapped_column(BigInteger, Identity(), unique=True, nullable=False)
    order_hash: Mapped[str] = mapped_column(Bytes32, unique=True, nullable=False)
    maker_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=False, index=True
    )
    maker: Mapped[str] = mapped_column(Address, nullable=False)  # lowercase
    market_id: Mapped[str] = mapped_column(Bytes32, nullable=False)
    side: Mapped[OrderSide] = mapped_column(_enum(OrderSide, "order_side"), nullable=False)
    amount_wh: Mapped[Decimal] = mapped_column(Uint256, nullable=False)
    limit_price: Mapped[Decimal] = mapped_column(Uint256, nullable=False)  # mUSDC (6dp) per kWh
    expiry: Mapped[int] = mapped_column(BigInteger, nullable=False)
    nonce: Mapped[Decimal] = mapped_column(Uint256, nullable=False)
    salt: Mapped[str] = mapped_column(Bytes32, nullable=False)
    signature: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[OrderStatus] = mapped_column(
        _enum(OrderStatus, "order_status"), default=OrderStatus.OPEN, nullable=False
    )
    filled_wh: Mapped[Decimal] = mapped_column(Uint256, default=Decimal(0), nullable=False)
    reserved_wh: Mapped[Decimal] = mapped_column(
        Uint256, default=Decimal(0), server_default="0", nullable=False
    )
    # Stable machine codes (e.g. "user", "unfunded:balance", "dust_remainder", "on_chain_cancel").
    cancel_reason: Mapped[str | None] = mapped_column(Text)
    # "offchain" (our book only; the signature stays valid on-chain) or "onchain" (binding).
    cancel_type: Mapped[str | None] = mapped_column(String(16))
    # A soft cancel asked while Wh were reserved: the unreserved remainder is already gone and the
    # order is finalized as cancelled once the in-flight settlements resolve.
    cancel_pending: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="false", nullable=False
    )

    __table_args__ = (
        CheckConstraint("filled_wh + reserved_wh <= amount_wh", name="not_overfilled"),
        CheckConstraint("filled_wh >= 0 AND reserved_wh >= 0", name="non_negative"),
        CheckConstraint("cancel_type IN ('offchain', 'onchain')", name="cancel_type_values"),
        Index("ix_orders_book", "market_id", "side", "status", "limit_price", "seq"),
        Index("ix_orders_maker_status", "maker", "status"),
        Index("ix_orders_expiry", "expiry"),
    )


class Trade(Base):
    """One matched pair and fill. Whether it really happened is the settlement's status; the
    `(tx_hash, log_index)` of the `TradeSettled` event is filled in by the chain listener."""

    __tablename__ = "trades"
    __table_args__ = (
        UniqueConstraint("tx_hash", "log_index", name="uq_trades_tx_log"),
        Index("ix_trades_market_executed", "market_id", "executed_at"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    market_id: Mapped[str] = mapped_column(Bytes32, nullable=False)
    buy_order_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("orders.id"), nullable=False, index=True
    )
    sell_order_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("orders.id"), nullable=False, index=True
    )
    buyer: Mapped[str] = mapped_column(Address, nullable=False, index=True)  # lowercase
    seller: Mapped[str] = mapped_column(Address, nullable=False, index=True)  # lowercase
    fill_wh: Mapped[Decimal] = mapped_column(Uint256, nullable=False)
    exec_price: Mapped[Decimal] = mapped_column(Uint256, nullable=False)
    cost: Mapped[Decimal] = mapped_column(Uint256, nullable=False)
    fee: Mapped[Decimal] = mapped_column(Uint256, nullable=False)
    tx_hash: Mapped[str | None] = mapped_column(Bytes32)
    log_index: Mapped[int | None] = mapped_column(Integer)
    block_number: Mapped[int | None] = mapped_column(BigInteger)
    executed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    # Settled on-chain by a transaction this API did not create (no settlement row).
    external: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="false", nullable=False
    )


class PriceTick(Base):
    """One pricing-engine tick, with the full price-drivers breakdown for the Insights page."""

    __tablename__ = "price_ticks"
    __table_args__ = (
        Index("ix_price_ticks_market_ts", "market_id", "ts"),
        UniqueConstraint("market_id", "ts", name="uq_price_ticks_market_ts"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    ts: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )  # chain/wall clock
    market_id: Mapped[str] = mapped_column(Bytes32, nullable=False)
    raw_price: Mapped[Decimal] = mapped_column(Uint256, nullable=False)  # mUSDC base units / kWh
    price: Mapped[Decimal] = mapped_column(Uint256, nullable=False)  # applied price
    ema_price: Mapped[Decimal] = mapped_column(Numeric(30, 6), nullable=False)
    supply_w: Mapped[Decimal] = mapped_column(Uint256, nullable=False)
    demand_w: Mapped[Decimal] = mapped_column(Uint256, nullable=False)
    band_bps: Mapped[int] = mapped_column(Integer, nullable=False)
    breaker_state: Mapped[str] = mapped_column(String(16), nullable=False)
    breaker_reason: Mapped[str | None] = mapped_column(Text)
    stale: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    weather_age_s: Mapped[int | None] = mapped_column(Integer)
    vol_bps: Mapped[Decimal | None] = mapped_column(Numeric(20, 4))
    market_time: Mapped[int] = mapped_column(
        BigInteger, nullable=False
    )  # market clock (may be simulated)
    quote_timestamp: Mapped[int | None] = mapped_column(BigInteger)  # chain clock the quote carries
    clock_mode: Mapped[str] = mapped_column(String(20), nullable=False)
    signature: Mapped[str | None] = mapped_column(Text)
    drivers: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)


class Market(Base, TimestampMixin):
    """A market and the region it covers (a bounding box; all null = everywhere)."""

    __tablename__ = "markets"

    id: Mapped[uuid.UUID] = uuid_pk()
    market_id: Mapped[str] = mapped_column(Bytes32, unique=True, nullable=False)  # keccak(name)
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    is_primary: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False
    )  # pushed to PriceOracle
    min_lat_e6: Mapped[int | None] = mapped_column(Integer)
    max_lat_e6: Mapped[int | None] = mapped_column(Integer)
    min_lon_e6: Mapped[int | None] = mapped_column(Integer)
    max_lon_e6: Mapped[int | None] = mapped_column(Integer)


class PricingParam(Base):
    """A parameter override. scope is a market id, or '*' for all markets."""

    __tablename__ = "pricing_params"
    __table_args__ = (UniqueConstraint("scope", "key", name="uq_pricing_params_scope_key"),)

    id: Mapped[uuid.UUID] = uuid_pk()
    scope: Mapped[str] = mapped_column(String(66), nullable=False)
    key: Mapped[str] = mapped_column(String(64), nullable=False)
    value: Mapped[Any] = mapped_column(JSONB, nullable=False)
    updated_by: Mapped[str] = mapped_column(Address, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class PricingParamChange(Base):
    """Append-only audit trail of parameter changes."""

    __tablename__ = "pricing_param_changes"
    __table_args__ = (Index("ix_pricing_param_changes_scope_key", "scope", "key"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    scope: Mapped[str] = mapped_column(String(66), nullable=False)
    key: Mapped[str] = mapped_column(String(64), nullable=False)
    old_value: Mapped[Any] = mapped_column(JSONB)
    new_value: Mapped[Any] = mapped_column(JSONB)
    changed_by: Mapped[str] = mapped_column(Address, nullable=False)
    changed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class PricingEvent(Base):
    """Breaker transitions and anomalies (the 'anomaly event' log)."""

    __tablename__ = "pricing_events"
    __table_args__ = (Index("ix_pricing_events_market_ts", "market_id", "ts"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    market_id: Mapped[str] = mapped_column(Bytes32, nullable=False)
    kind: Mapped[str] = mapped_column(String(40), nullable=False)
    details: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)


class Settlement(Base, TimestampMixin):
    """The settlement state machine: matched -> submitted -> confirmed | failed.

    Only the chain listener sets `confirmed`. `failed` always carries `failure_reason`.
    """

    __tablename__ = "settlements"
    __table_args__ = (
        Index("ix_settlements_status", "status"),
        Index("ix_settlements_hashes", "buy_hash", "sell_hash"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    trade_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("trades.id"), unique=True, nullable=False
    )
    market_id: Mapped[str] = mapped_column(Bytes32, nullable=False)
    buy_hash: Mapped[str] = mapped_column(Bytes32, nullable=False)
    sell_hash: Mapped[str] = mapped_column(Bytes32, nullable=False)
    status: Mapped[SettlementStatus] = mapped_column(
        _enum(SettlementStatus, "settlement_status"),
        default=SettlementStatus.MATCHED,
        nullable=False,
    )
    # The matcher's price is provisional; the final one is fixed when the tx is signed.
    provisional_exec_price: Mapped[Decimal] = mapped_column(Uint256, nullable=False)
    exec_price: Mapped[Decimal | None] = mapped_column(Uint256)
    quote_price: Mapped[Decimal | None] = mapped_column(Uint256)
    quote_timestamp: Mapped[int | None] = mapped_column(BigInteger)
    # Signed raw tx saved BEFORE broadcast so a crash re-sends the same tx (never a new nonce).
    raw_tx: Mapped[str | None] = mapped_column(Text)
    tx_nonce: Mapped[int | None] = mapped_column(BigInteger)
    tx_hash: Mapped[str | None] = mapped_column(Bytes32)  # latest
    tx_hashes: Mapped[list[str]] = mapped_column(
        JSONB, default=list, server_default="[]", nullable=False
    )  # incl. fee-bump replacements
    block_number: Mapped[int | None] = mapped_column(BigInteger)
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    failure_reason: Mapped[str | None] = mapped_column(Text)


class PairCooldown(Base):
    """Exponential-backoff cooldown for a (buy, sell) pair the relayer had to release."""

    __tablename__ = "pair_cooldowns"

    buy_hash: Mapped[str] = mapped_column(Bytes32, primary_key=True)
    sell_hash: Mapped[str] = mapped_column(Bytes32, primary_key=True)
    failures: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    until: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_reason: Mapped[str] = mapped_column(Text, nullable=False)


class ChainEvent(Base):
    """An exchange/token log the listener has applied. UNIQUE(tx_hash, log_index) = idempotency."""

    __tablename__ = "chain_events"
    __table_args__ = (
        UniqueConstraint("tx_hash", "log_index", name="uq_chain_events_tx_log"),
        Index("ix_chain_events_block", "block_number"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    tx_hash: Mapped[str] = mapped_column(Bytes32, nullable=False)
    log_index: Mapped[int] = mapped_column(Integer, nullable=False)
    block_number: Mapped[int] = mapped_column(BigInteger, nullable=False)
    block_hash: Mapped[str] = mapped_column(Bytes32, nullable=False)
    address: Mapped[str] = mapped_column(Address, nullable=False)
    name: Mapped[str] = mapped_column(String(40), nullable=False)
    args: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    # What applying the event changed, so a reorg can reverse it exactly.
    effects: Mapped[dict[str, Any]] = mapped_column(
        JSONB, default=dict, server_default="{}", nullable=False
    )


class IndexerBlock(Base):
    """Hash of every block the listener applied (recent window), to detect reorgs."""

    __tablename__ = "indexer_blocks"

    block_number: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    block_hash: Mapped[str] = mapped_column(Bytes32, nullable=False)


class IndexerState(Base):
    """Listener cursor: highest block whose logs are fully applied."""

    __tablename__ = "indexer_state"

    name: Mapped[str] = mapped_column(String(40), primary_key=True)
    last_block: Mapped[int] = mapped_column(BigInteger, nullable=False)
