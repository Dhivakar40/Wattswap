"""ORM tables. `orders`, `trades`, `price_ticks` and `settlements` are schema-only in Phase 2."""

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy import Enum as SAEnum
from sqlalchemy.dialects.postgresql import UUID
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
    """Schema only (Phase 4)."""

    __tablename__ = "orders"

    id: Mapped[uuid.UUID] = uuid_pk()
    order_hash: Mapped[str] = mapped_column(Bytes32, unique=True, nullable=False)
    maker_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=False, index=True
    )
    maker: Mapped[str] = mapped_column(Address, nullable=False)
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
    cancel_reason: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (
        CheckConstraint("filled_wh <= amount_wh", name="not_overfilled"),
        Index("ix_orders_book", "market_id", "side", "status", "limit_price", "created_at"),
    )


class Trade(Base):
    """Schema only (Phase 4)."""

    __tablename__ = "trades"
    __table_args__ = (UniqueConstraint("tx_hash", "log_index", name="uq_trades_tx_log"),)

    id: Mapped[uuid.UUID] = uuid_pk()
    buy_order_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("orders.id"), nullable=False, index=True
    )
    sell_order_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("orders.id"), nullable=False, index=True
    )
    fill_wh: Mapped[Decimal] = mapped_column(Uint256, nullable=False)
    exec_price: Mapped[Decimal] = mapped_column(Uint256, nullable=False)
    cost: Mapped[Decimal] = mapped_column(Uint256, nullable=False)
    fee: Mapped[Decimal] = mapped_column(Uint256, nullable=False)
    tx_hash: Mapped[str | None] = mapped_column(Bytes32)
    log_index: Mapped[int | None] = mapped_column(Integer)
    block_number: Mapped[int | None] = mapped_column(BigInteger)
    executed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class PriceTick(Base):
    """Schema only (Phase 3)."""

    __tablename__ = "price_ticks"
    __table_args__ = (Index("ix_price_ticks_market_ts", "market_id", "ts"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    market_id: Mapped[str] = mapped_column(Bytes32, nullable=False)
    raw_price: Mapped[Decimal] = mapped_column(Uint256, nullable=False)
    price: Mapped[Decimal] = mapped_column(Uint256, nullable=False)
    supply_wh: Mapped[Decimal] = mapped_column(Uint256, nullable=False)
    demand_wh: Mapped[Decimal] = mapped_column(Uint256, nullable=False)
    stale: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    signature: Mapped[str | None] = mapped_column(Text)


class Settlement(Base, TimestampMixin):
    """Schema only (Phase 4)."""

    __tablename__ = "settlements"

    id: Mapped[uuid.UUID] = uuid_pk()
    trade_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("trades.id"), unique=True, nullable=False
    )
    status: Mapped[SettlementStatus] = mapped_column(
        _enum(SettlementStatus, "settlement_status"),
        default=SettlementStatus.MATCHED,
        nullable=False,
    )
    tx_hash: Mapped[str | None] = mapped_column(Bytes32)
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    failure_reason: Mapped[str | None] = mapped_column(Text)
