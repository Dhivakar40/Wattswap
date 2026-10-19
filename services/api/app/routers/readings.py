"""Reading ingest (device-signed) and mint history."""

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request, Response
from sqlalchemy import select, tuple_

from app.auth.deps import SessionDep, UserDep
from app.core.pagination import CURSOR_DOC, decode_cursor, encode_cursor
from app.core.ratelimit import hit, ip_limit
from app.core.state import ChainDep, RedisDep, SettingsDep
from app.models import Meter, Mint, Reading, User, UserRole
from app.realtime import meter_events
from app.schemas.api import MintItem, MintsPage, ReadingAccepted, ReadingIn
from app.services.readings import ingest_reading, mint_out

router = APIRouter(tags=["readings"])


@router.post(
    "/readings",
    response_model=ReadingAccepted,
    status_code=202,
    dependencies=[Depends(ip_limit("ingest"))],
)
async def post_reading(
    body: ReadingIn,
    request: Request,
    response: Response,
    session: SessionDep,
    chain: ChainDep,
    redis: RedisDep,
    settings: SettingsDep,
) -> ReadingAccepted:
    """Ingest an EIP-712 signed reading from a meter.

    Authenticity is the device signature, not a session cookie. Re-sending the same reading
    is safe: it returns 200 with the existing record and never queues a second mint.
    """
    await hit(
        redis,
        f"ingest:meter:{body.meter_id}",
        settings.rate_limit_meter_ingest,
        settings.rate_limit_window_seconds,
    )
    reading, duplicate = await ingest_reading(session, chain, body)
    if duplicate:
        response.status_code = 200
    else:
        simulated = await session.scalar(
            select(Meter.is_simulated).where(Meter.meter_id == body.meter_id)
        )
        await meter_events.publish_reading(redis, reading, simulated=bool(simulated))
    return ReadingAccepted(reading=reading, duplicate=duplicate)


@router.get("/mints", response_model=MintsPage)
async def list_mints(
    user: UserDep,
    session: SessionDep,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    before: Annotated[str | None, Query(description=CURSOR_DOC)] = None,
) -> MintsPage:
    stmt = (
        select(Mint, Reading, Meter.meter_id)
        .join(Reading, Reading.id == Mint.reading_pk)
        .join(Meter, Meter.id == Reading.meter_pk)
        .order_by(Mint.created_at.desc(), Mint.id.desc())
        .limit(limit + 1)
    )
    if user.role != UserRole.ADMIN:
        stmt = stmt.join(User, User.id == Meter.owner_id).where(User.id == user.id)
    if before is not None:
        c = decode_cursor(before, key="time", id_type=uuid.UUID)
        stmt = stmt.where(tuple_(Mint.created_at, Mint.id) < tuple_(c.key, c.id))
    rows = (await session.execute(stmt)).all()
    page = rows[:limit]
    return MintsPage(
        items=[
            MintItem(
                reading_id=r.reading_id,
                meter_id=meter_id,
                amount_wh=r.amount_wh,
                interval_start=r.interval_start,
                interval_seconds=r.interval_seconds,
                mint=mint_out(m),
            )
            for m, r, meter_id in page
        ],
        next_before=(
            encode_cursor(page[-1][0].created_at, page[-1][0].id) if len(rows) > limit else None
        ),
    )
