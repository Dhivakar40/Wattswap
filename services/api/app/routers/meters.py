"""Meter registry endpoints."""

import uuid
from typing import Annotated

from fastapi import APIRouter, Query
from sqlalchemy import select, tuple_

from app.auth.deps import SessionDep, UserDep
from app.core.pagination import CURSOR_DOC, decode_cursor, encode_cursor
from app.core.state import ChainDep, SettingsDep
from app.models import Meter, Mint, Reading, User, UserRole
from app.schemas.api import MeterCreate, MeterOut, MeterPatch, ReadingsPage
from app.services.meters import (
    get_owned_meter,
    meter_out,
    minted_wh_for,
    register_meter,
    rotate_device_signer,
    set_meter_active,
)
from app.services.readings import reading_out

router = APIRouter(prefix="/meters", tags=["meters"])


@router.post("", response_model=MeterOut, status_code=201)
async def create_meter(
    body: MeterCreate, user: UserDep, session: SessionDep, chain: ChainDep, settings: SettingsDep
) -> MeterOut:
    """Register a (simulated) meter on-chain. The user supplies only the device *address*;
    the device key never leaves the device."""
    meter = await register_meter(session, chain, settings, user, body)
    return meter_out(meter, user.address, 0)


@router.get("", response_model=list[MeterOut])
async def list_meters(user: UserDep, session: SessionDep) -> list[MeterOut]:
    stmt = select(Meter, User.address).join(User, User.id == Meter.owner_id)
    if user.role != UserRole.ADMIN:
        stmt = stmt.where(Meter.owner_id == user.id)
    rows = (await session.execute(stmt.order_by(Meter.created_at.desc()))).all()
    minted = await minted_wh_for(session, [r[0].id for r in rows])
    return [meter_out(m, addr, minted.get(m.id, 0)) for m, addr in rows]


@router.get("/{meter_ref}", response_model=MeterOut)
async def get_meter(meter_ref: str, user: UserDep, session: SessionDep) -> MeterOut:
    meter, owner = await get_owned_meter(session, user, meter_ref)
    minted = await minted_wh_for(session, [meter.id])
    return meter_out(meter, owner, minted.get(meter.id, 0))


@router.patch("/{meter_ref}", response_model=MeterOut)
async def patch_meter(
    meter_ref: str,
    body: MeterPatch,
    user: UserDep,
    session: SessionDep,
    chain: ChainDep,
    settings: SettingsDep,
) -> MeterOut:
    meter, owner = await get_owned_meter(session, user, meter_ref)
    if body.label is not None:
        meter.label = body.label
        await session.commit()
    if body.active is not None:
        await set_meter_active(session, chain, settings, meter, body.active)
    if body.device_signer is not None:
        await rotate_device_signer(session, chain, settings, meter, body.device_signer)
    minted = await minted_wh_for(session, [meter.id])
    return meter_out(meter, owner, minted.get(meter.id, 0))


@router.get("/{meter_ref}/readings", response_model=ReadingsPage)
async def list_readings(
    meter_ref: str,
    user: UserDep,
    session: SessionDep,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    before: Annotated[str | None, Query(description=CURSOR_DOC)] = None,
) -> ReadingsPage:
    """Readings newest-first with their mint status; page with `next_before`."""
    meter, _ = await get_owned_meter(session, user, meter_ref)
    stmt = (
        select(Reading, Mint)
        .outerjoin(Mint, Mint.reading_pk == Reading.id)
        .where(Reading.meter_pk == meter.id)
        .order_by(Reading.interval_start.desc(), Reading.id.desc())
        .limit(limit + 1)
    )
    if before is not None:
        c = decode_cursor(before, key="int", id_type=uuid.UUID)
        stmt = stmt.where(tuple_(Reading.interval_start, Reading.id) < tuple_(c.key, c.id))
    rows = (await session.execute(stmt)).all()
    page = rows[:limit]
    next_before = (
        encode_cursor(page[-1][0].interval_start, page[-1][0].id) if len(rows) > limit else None
    )
    return ReadingsPage(
        items=[reading_out(r, meter.meter_id, m) for r, m in page], next_before=next_before
    )
