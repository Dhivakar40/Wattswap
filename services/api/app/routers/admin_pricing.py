"""Admin: pricing parameters (validated and audited), breaker status and reset, markets."""

import json
from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Query, Request
from sqlalchemy import select

from app.auth.deps import AdminDep, SessionDep
from app.core.errors import AppError
from app.core.state import RedisDep
from app.models import Market, PricingEvent, PricingParam, PricingParamChange
from app.pricing import publish, repo
from app.pricing.params import (
    READONLY,
    SPECS,
    ParamError,
    PricingParams,
    from_mapping,
)
from app.routers.markets import market_out, require_market
from app.schemas.pricing import (
    BreakerResetIn,
    ConfigEntry,
    ConfigOut,
    ConfigUpdate,
    MarketCreate,
    MarketOut,
    MarketPatch,
)

router = APIRouter(prefix="/admin", tags=["admin"])


def _scope(market_id: str | None) -> str:
    return repo.GLOBAL_SCOPE if market_id is None else market_id.lower()


async def _layers(
    session: SessionDep, market_id: str | None
) -> tuple[dict[str, Any], dict[str, Any]]:
    """(global overrides, this market's overrides) as stored."""
    rows = await session.scalars(select(PricingParam))
    glob: dict[str, Any] = {}
    mine: dict[str, Any] = {}
    for r in rows:
        if r.scope == repo.GLOBAL_SCOPE:
            glob[r.key] = r.value
        elif market_id is not None and r.scope == market_id:
            mine[r.key] = r.value
    return glob, mine


@router.get("/pricing/config", response_model=ConfigOut)
async def get_config(
    _: AdminDep,
    session: SessionDep,
    market_id: Annotated[str | None, Query(description="omit for the global scope")] = None,
) -> ConfigOut:
    if market_id is not None:
        await require_market(session, market_id)
        market_id = market_id.lower()
    glob, mine = await _layers(session, market_id)
    defaults = PricingParams().to_mapping()
    effective = from_mapping({**glob, **mine}).to_mapping()
    changes = {}
    for ch in await session.scalars(
        select(PricingParamChange)
        .where(
            PricingParamChange.scope.in_([repo.GLOBAL_SCOPE, *([market_id] if market_id else [])])
        )
        .order_by(PricingParamChange.id)
    ):
        changes[(ch.scope, ch.key)] = {
            "scope": ch.scope,
            "old": ch.old_value,
            "new": ch.new_value,
            "by": ch.changed_by,
            "at": ch.changed_at.isoformat(),
        }
    entries = []
    for key, spec in SPECS.items():
        source = "market" if key in mine else "global" if key in glob else "default"
        scope_of = market_id if key in mine else repo.GLOBAL_SCOPE
        entries.append(
            ConfigEntry(
                key=key,
                kind=spec.kind,
                unit=spec.unit,
                description=spec.description,
                default=defaults[key],
                value=effective[key],
                source=source,
                overridden=source != "default",
                minimum=None if spec.minimum is None else str(spec.minimum),
                maximum=None if spec.maximum is None else str(spec.maximum),
                editable=key not in READONLY,
                last_change=changes.get((scope_of or "", key)),
            )
        )
    return ConfigOut(market_id=market_id, scope=_scope(market_id), entries=entries)


@router.put("/pricing/config", response_model=ConfigOut)
async def put_config(
    body: ConfigUpdate, admin: AdminDep, session: SessionDep, request: Request
) -> ConfigOut:
    market_id = None if body.market_id is None else body.market_id.lower()
    if market_id is not None:
        await require_market(session, market_id)
    scope = _scope(market_id)
    glob, mine = await _layers(session, market_id)
    current = mine if market_id else glob
    unknown = [k for k in body.reset if k not in SPECS]
    if unknown:
        raise AppError(422, "validation_error", "unknown parameter(s) in reset", {"keys": unknown})

    new_scope = {k: v for k, v in current.items() if k not in body.reset} | body.values
    layered = (glob | new_scope) if market_id else new_scope
    # validate the layered result; readonly keys can never be set through the API
    if READONLY & set(body.values):
        raise AppError(
            422,
            "validation_error",
            "invalid pricing parameters",
            {
                "errors": {
                    k: "read-only here; set PRICING_TICK_SECONDS in the environment"
                    for k in READONLY & set(body.values)
                }
            },
        )
    tick_seconds = request.app.state.settings.pricing_tick_seconds
    try:
        from_mapping(layered, base=PricingParams(tick_seconds=tick_seconds))
    except ParamError as exc:
        raise AppError(
            422, "validation_error", "invalid pricing parameters", {"errors": exc.errors}
        ) from exc

    now = datetime.now(UTC)
    for key in body.reset:
        await repo.clear_override(session, scope, key, admin.address, now)
    # store JSON-native values: Decimals arrive as strings, ints as ints
    await repo.set_overrides(session, scope, dict(body.values), admin.address, now)
    return await get_config(admin, session, market_id)


@router.get("/pricing/state")
async def pricing_state(
    _: AdminDep, session: SessionDep, redis: RedisDep, market_id: Annotated[str, Query()]
) -> dict[str, Any]:
    """Breaker, per-cell weather age, last tick and last oracle push (written by the engine)."""
    market = await require_market(session, market_id)
    raw = await redis.get(publish.state_key(market.market_id))
    if not raw:
        raise AppError(404, "no_state_yet", "the pricing engine has not reported yet")
    state: dict[str, Any] = json.loads(raw)
    return state


@router.post("/pricing/breaker/reset", status_code=202)
async def reset_breaker(
    body: BreakerResetIn, admin: AdminDep, session: SessionDep, redis: RedisDep
) -> dict[str, Any]:
    """Queue a manual breaker reset; the engine applies it on its next tick."""
    market = await require_market(session, body.market_id)
    await redis.set(publish.command_key(market.market_id), "reset_breaker", ex=300)
    session.add(
        PricingEvent(
            ts=datetime.now(UTC),
            market_id=market.market_id,
            kind="breaker_reset_requested",
            details={"by": admin.address},
        )
    )
    await session.commit()
    return {"queued": True, "market_id": market.market_id}


@router.get("/pricing/events")
async def pricing_events(
    _: AdminDep,
    session: SessionDep,
    market_id: Annotated[str, Query()],
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
) -> list[dict[str, Any]]:
    """Breaker transitions and anomaly events, newest first."""
    market = await require_market(session, market_id)
    rows = await session.scalars(
        select(PricingEvent)
        .where(PricingEvent.market_id == market.market_id)
        .order_by(PricingEvent.id.desc())
        .limit(limit)
    )
    return [{"ts": int(r.ts.timestamp()), "kind": r.kind, "details": r.details} for r in rows]


# ------------------------------------------------------------------------------ markets
@router.post("/markets", response_model=MarketOut, status_code=201)
async def create_market(
    body: MarketCreate, _: AdminDep, session: SessionDep, request: Request
) -> MarketOut:
    """Define a market. Its id is keccak256(name), the convention the exchange uses; it also
    has to be enabled on-chain by the exchange admin (`setMarket`)."""
    mid = repo.market_id_for(body.name)
    if await repo.get_market(session, mid) is not None:
        raise AppError(409, "market_exists", "a market with this name already exists")
    _check_box(body.min_lat_e6, body.max_lat_e6, body.min_lon_e6, body.max_lon_e6)
    if body.is_primary:
        await _clear_primary(session)
    m = Market(
        market_id=mid,
        name=body.name,
        enabled=body.enabled,
        is_primary=body.is_primary,
        min_lat_e6=body.min_lat_e6,
        max_lat_e6=body.max_lat_e6,
        min_lon_e6=body.min_lon_e6,
        max_lon_e6=body.max_lon_e6,
    )
    session.add(m)
    await session.commit()
    return await market_out(request, m)


@router.patch("/markets/{market_id}", response_model=MarketOut)
async def patch_market(
    market_id: str, body: MarketPatch, _: AdminDep, session: SessionDep, request: Request
) -> MarketOut:
    m = await require_market(session, market_id)
    data = body.model_dump(exclude_unset=True)
    box = {
        k: data.get(k, getattr(m, k))
        for k in ("min_lat_e6", "max_lat_e6", "min_lon_e6", "max_lon_e6")
    }
    _check_box(**box)
    if data.get("is_primary"):
        await _clear_primary(session)
    for key, value in data.items():
        setattr(m, key, value)
    await session.commit()
    return await market_out(request, m)


def _check_box(
    min_lat_e6: int | None, max_lat_e6: int | None, min_lon_e6: int | None, max_lon_e6: int | None
) -> None:
    if (min_lat_e6 is not None and max_lat_e6 is not None and min_lat_e6 > max_lat_e6) or (
        min_lon_e6 is not None and max_lon_e6 is not None and min_lon_e6 > max_lon_e6
    ):
        raise AppError(422, "validation_error", "region minimum must not exceed its maximum")


async def _clear_primary(session: SessionDep) -> None:
    for other in await session.scalars(select(Market).where(Market.is_primary.is_(True))):
        other.is_primary = False
