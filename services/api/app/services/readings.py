"""Reading ingest: verify a device-signed reading, store it once, queue its mint."""

import logging
import time
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.chain.eip712 import ReadingData, derive_reading_id, recover_reading_signer
from app.core.errors import AppError
from app.core.metrics import READINGS_INGESTED
from app.core.state import ChainServices
from app.models import Meter, Mint, MintStatus, Reading, RegisterStatus
from app.schemas.api import MintOut, ReadingIn, ReadingOut

log = logging.getLogger("wattswap.readings")


def mint_out(mint: Mint) -> MintOut:
    return MintOut(
        status=mint.status.value,
        tx_hash=mint.tx_hash,
        block_number=mint.block_number,
        attempts=mint.attempts,
        failure_reason=mint.failure_reason,
        confirmed_at=mint.confirmed_at,
    )


def reading_out(reading: Reading, meter_id: str, mint: Mint | None) -> ReadingOut:
    return ReadingOut(
        reading_id=reading.reading_id,
        meter_id=meter_id,
        amount_wh=reading.amount_wh,
        interval_start=reading.interval_start,
        interval_seconds=reading.interval_seconds,
        received_at=reading.received_at,
        mint=mint_out(mint) if mint else None,
    )


def _reject(result: str, error: AppError) -> AppError:
    READINGS_INGESTED.labels(result=result).inc()
    return error


async def ingest_reading(
    session: AsyncSession, chain: ChainServices, body: ReadingIn, *, now: float | None = None
) -> tuple[ReadingOut, bool]:
    """Returns (reading, duplicate). Idempotent on reading_id.

    Layers: the meter row is locked (`FOR UPDATE`) so ingest is serialised per meter; the
    database enforces UNIQUE(reading_id) and UNIQUE(meter, interval_start); and the contract
    enforces `readingMinted` and `lastIntervalEnd`.
    """
    meter = await session.scalar(
        select(Meter).where(Meter.meter_id == body.meter_id).with_for_update()
    )
    if meter is None:
        raise _reject("unknown_meter", AppError(404, "unknown_meter", "meter is not registered"))
    if meter.register_status != RegisterStatus.CONFIRMED:
        raise _reject(
            "meter_not_registered",
            AppError(409, "meter_not_registered", "meter registration is not confirmed on-chain"),
        )
    if not meter.active:
        raise _reject("meter_inactive", AppError(409, "meter_inactive", "meter is deactivated"))

    data = ReadingData(
        meter_id=body.meter_id,
        reading_id=body.reading_id,
        amount_wh=body.amount_wh,
        interval_start=body.interval_start,
        interval_seconds=body.interval_seconds,
    )
    if body.reading_id != derive_reading_id(
        body.meter_id, body.interval_start, body.interval_seconds
    ):
        raise _reject(
            "bad_reading_id",
            AppError(
                422,
                "reading_id_mismatch",
                "reading_id must equal keccak256(abi.encode(meterId, intervalStart, intervalSeconds))",
            ),
        )

    signer = recover_reading_signer(
        chain.client.settings.chain_id, chain.client.token_address, data, body.signature
    )
    if signer is None or signer.lower() != meter.device_signer:
        raise _reject(
            "bad_signature",
            AppError(401, "invalid_signature", "reading is not signed by the meter's device key"),
        )

    existing = await session.scalar(select(Reading).where(Reading.reading_id == body.reading_id))
    if existing is not None:
        same = (
            existing.amount_wh == body.amount_wh
            and existing.interval_start == body.interval_start
            and existing.interval_seconds == body.interval_seconds
        )
        if not same:
            raise _reject(
                "conflict",
                AppError(409, "reading_conflict", "reading_id already used with different data"),
            )
        mint = await session.scalar(select(Mint).where(Mint.reading_pk == existing.id))
        READINGS_INGESTED.labels(result="duplicate").inc()
        return reading_out(existing, meter.meter_id, mint), True

    params = await chain.client.plausibility()
    if not params.min_interval_seconds <= body.interval_seconds <= params.max_interval_seconds:
        raise _reject(
            "bad_interval",
            AppError(
                422,
                "invalid_interval",
                "interval length is outside the registry's allowed range",
                {"min": params.min_interval_seconds, "max": params.max_interval_seconds},
            ),
        )
    end = body.interval_start + body.interval_seconds
    current = time.time() if now is None else now
    if end > current:
        raise _reject(
            "interval_not_complete",
            AppError(422, "interval_not_complete", "the interval has not ended yet"),
        )
    # Uses the reading's own interval length, never an assumed default.
    max_wh = params.max_energy_wh(meter.rated_capacity_w, body.interval_seconds)
    if body.amount_wh > max_wh:
        raise _reject(
            "implausible",
            AppError(
                422,
                "implausible_reading",
                "amount exceeds what the meter's rated capacity can produce in this interval",
                {"max_wh": max_wh, "amount_wh": body.amount_wh},
            ),
        )

    db_last = await session.scalar(
        select(func.max(Reading.interval_start + Reading.interval_seconds)).where(
            Reading.meter_pk == meter.id
        )
    )
    chain_last = await chain.client.last_interval_end(meter.meter_id)
    last_end = max(int(db_last or 0), chain_last)
    if body.interval_start < last_end:
        raise _reject(
            "out_of_order",
            AppError(
                409,
                "interval_out_of_order",
                "readings must be sent in order and must not overlap; backfill oldest-first",
                {"last_interval_end": last_end, "interval_start": body.interval_start},
            ),
        )

    reading = Reading(
        reading_id=body.reading_id,
        meter_pk=meter.id,
        amount_wh=body.amount_wh,
        interval_start=body.interval_start,
        interval_seconds=body.interval_seconds,
        signature=body.signature,
        received_at=datetime.now(UTC),
    )
    session.add(reading)
    try:
        await session.flush()
        mint = Mint(reading_pk=reading.id, status=MintStatus.PENDING)
        session.add(mint)
        await session.commit()
    except IntegrityError:
        await session.rollback()
        raced = await session.scalar(select(Reading).where(Reading.reading_id == body.reading_id))
        if raced is None:
            raise _reject(
                "conflict", AppError(409, "reading_conflict", "conflicting reading exists")
            ) from None
        raced_mint = await session.scalar(select(Mint).where(Mint.reading_pk == raced.id))
        READINGS_INGESTED.labels(result="duplicate").inc()
        return reading_out(raced, body.meter_id, raced_mint), True

    READINGS_INGESTED.labels(result="accepted").inc()
    log.info(
        "reading accepted",
        extra={"reading_id": reading.reading_id, "meter_id": meter.meter_id, "wh": body.amount_wh},
    )
    return reading_out(reading, meter.meter_id, mint), False
