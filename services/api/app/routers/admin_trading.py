"""Admin: trading status (leaders, queue depth, listener lag) and the matching pause switch."""

from typing import Any

from fastapi import APIRouter
from sqlalchemy import func, select

from app.auth.deps import AdminDep, SessionDep
from app.core.state import ChainDep, RedisDep
from app.matching import lifecycle
from app.matching.matcher import PAUSE_KEY
from app.models import IndexerState, Order, PairCooldown, Settlement
from app.workers.listener import CURSOR
from app.workers.relayer import HALT_KEY

router = APIRouter(prefix="/admin/trading", tags=["admin"])

ROLES = ("matcher", "relayer", "listener", "sweeper")


async def _state(session: SessionDep, redis: RedisDep, chain: ChainDep) -> dict[str, Any]:
    by_status = {
        str(status.value): int(n)
        for status, n in (
            await session.execute(
                select(Settlement.status, func.count()).group_by(Settlement.status)
            )
        ).all()
    }
    open_orders = int(
        await session.scalar(
            select(func.count()).select_from(Order).where(Order.status.in_(lifecycle.ACTIVE))
        )
        or 0
    )
    cursor = await session.get(IndexerState, CURSOR)
    head = int(await chain.client.w3.eth.block_number)
    cooling = int(
        await session.scalar(
            select(func.count())
            .select_from(PairCooldown)
            .where(PairCooldown.until > lifecycle.now())
        )
        or 0
    )
    return {
        "leaders": {role: bool(await redis.exists(f"lock:{role}")) for role in ROLES},
        "matching_paused": bool(await redis.get(PAUSE_KEY)),
        "relayer_halt": await redis.get(HALT_KEY),
        "settlements": by_status,
        "open_orders": open_orders,
        "pairs_on_cooldown": cooling,
        "listener": {
            "cursor": None if cursor is None else cursor.last_block,
            "head": head,
            "lag_blocks": head - (cursor.last_block if cursor else 0),
        },
    }


@router.get("/state")
async def state(
    _: AdminDep, session: SessionDep, redis: RedisDep, chain: ChainDep
) -> dict[str, Any]:
    return await _state(session, redis, chain)


@router.post("/pause")
async def pause(
    _: AdminDep, session: SessionDep, redis: RedisDep, chain: ChainDep
) -> dict[str, Any]:
    """Stop matching and submitting new settlements. In-flight transactions are still tracked."""
    await redis.set(PAUSE_KEY, "1")
    return await _state(session, redis, chain)


@router.post("/resume")
async def resume(
    _: AdminDep, session: SessionDep, redis: RedisDep, chain: ChainDep
) -> dict[str, Any]:
    await redis.delete(PAUSE_KEY, HALT_KEY)
    return await _state(session, redis, chain)
