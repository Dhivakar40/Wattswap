"""Meter registry operations (DB + on-chain MeterRegistry)."""

import logging
import secrets
import uuid
from decimal import Decimal

from eth_utils import keccak, to_bytes, to_checksum_address
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.chain.tx import TxRevertedError
from app.core.config import Settings
from app.core.errors import AppError
from app.core.state import ChainServices
from app.models import Meter, Mint, MintStatus, Reading, RegisterStatus, User, UserRole
from app.schemas.api import MeterCreate, MeterOut
from app.schemas.common import checksum

log = logging.getLogger("wattswap.meters")

E6 = 1_000_000


def derive_meter_id(owner: str, device_signer: str, salt: bytes) -> str:
    """meterId = keccak256(owner || deviceSigner || salt): unique per registration."""
    packed = to_bytes(hexstr=owner) + to_bytes(hexstr=device_signer) + salt
    return "0x" + keccak(packed).hex()


def to_e6(degrees: float) -> int:
    return int((Decimal(str(degrees)) * E6).to_integral_value())


def meter_out(meter: Meter, owner_address: str, minted_wh: int) -> MeterOut:
    return MeterOut(
        id=meter.id,
        meter_id=meter.meter_id,
        owner=checksum(owner_address),
        device_signer=checksum(meter.device_signer),
        label=meter.label,
        rated_capacity_w=meter.rated_capacity_w,
        lat=meter.lat_e6 / E6,
        lon=meter.lon_e6 / E6,
        active=meter.active,
        is_simulated=meter.is_simulated,
        register_status=meter.register_status.value,
        register_tx_hash=meter.register_tx_hash,
        register_error=meter.register_error,
        minted_wh=minted_wh,
        created_at=meter.created_at,
    )


async def minted_wh_for(session: AsyncSession, meter_pks: list[uuid.UUID]) -> dict[uuid.UUID, int]:
    if not meter_pks:
        return {}
    rows = await session.execute(
        select(Reading.meter_pk, func.coalesce(func.sum(Reading.amount_wh), 0))
        .join(Mint, Mint.reading_pk == Reading.id)
        .where(Reading.meter_pk.in_(meter_pks), Mint.status == MintStatus.CONFIRMED)
        .group_by(Reading.meter_pk)
    )
    return {pk: int(total) for pk, total in rows.all()}


def _require_registrar(chain: ChainServices) -> None:
    if chain.registrar is None:
        raise AppError(503, "registrar_unavailable", "meter registration is not configured")


async def register_meter(
    session: AsyncSession, chain: ChainServices, settings: Settings, user: User, body: MeterCreate
) -> Meter:
    _require_registrar(chain)
    assert chain.registrar is not None  # narrowed by _require_registrar
    meter = Meter(
        meter_id=derive_meter_id(user.address, body.device_signer, secrets.token_bytes(32)),
        owner_id=user.id,
        device_signer=body.device_signer,
        label=body.label,
        rated_capacity_w=body.rated_capacity_w,
        lat_e6=to_e6(body.lat),
        lon_e6=to_e6(body.lon),
        active=True,
        is_simulated=True,
        register_status=RegisterStatus.PENDING,
    )
    session.add(meter)
    await session.commit()

    fn = chain.client.registry.functions.registerMeter(
        meter.meter_id,
        to_checksum_address(user.address),
        to_checksum_address(body.device_signer),
        meter.rated_capacity_w,
        meter.lat_e6,
        meter.lon_e6,
    )
    try:
        rcpt = await chain.registrar.send_and_wait(
            fn, settings.mint_tx_timeout_seconds, settings.mint_confirmations
        )
    except (TxRevertedError, TimeoutError) as exc:
        meter.register_status = RegisterStatus.FAILED
        meter.register_error = str(exc)[:500]
        await session.commit()
        raise AppError(
            502,
            "registration_failed",
            "on-chain meter registration failed",
            {"meter_id": meter.meter_id, "reason": str(exc)[:500]},
        ) from exc
    meter.register_status = RegisterStatus.CONFIRMED
    meter.register_tx_hash = "0x" + bytes(rcpt["transactionHash"]).hex()
    await session.commit()
    log.info("meter registered", extra={"meter_id": meter.meter_id, "tx": meter.register_tx_hash})
    return meter


async def set_meter_active(
    session: AsyncSession, chain: ChainServices, settings: Settings, meter: Meter, active: bool
) -> None:
    _require_registrar(chain)
    assert chain.registrar is not None
    if meter.register_status != RegisterStatus.CONFIRMED:
        raise AppError(409, "meter_not_registered", "meter is not registered on-chain")
    if meter.active == active:
        return
    fn = chain.client.registry.functions.setActive(meter.meter_id, active)
    try:
        await chain.registrar.send_and_wait(
            fn, settings.mint_tx_timeout_seconds, settings.mint_confirmations
        )
    except (TxRevertedError, TimeoutError) as exc:
        raise AppError(
            502, "chain_error", "on-chain update failed", {"reason": str(exc)[:500]}
        ) from exc
    meter.active = active
    await session.commit()


async def get_owned_meter(session: AsyncSession, user: User, meter_ref: str) -> tuple[Meter, str]:
    """Meter by uuid or bytes32 id, only if the user owns it (admins can see all).

    Unowned meters are reported as 404, not 403, so ids cannot be probed.
    """
    stmt = select(Meter, User.address).join(User, User.id == Meter.owner_id)
    try:
        stmt = stmt.where(Meter.id == uuid.UUID(meter_ref))
    except ValueError:
        stmt = stmt.where(Meter.meter_id == meter_ref.lower())
    row = (await session.execute(stmt)).first()
    if row is None or (row[0].owner_id != user.id and user.role != UserRole.ADMIN):
        raise AppError(404, "not_found", "meter not found")
    return row[0], row[1]
