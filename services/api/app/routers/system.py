"""Liveness, readiness, metrics and the public config document."""

import logging

from fastapi import APIRouter, Request, Response
from fastapi.responses import JSONResponse
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from sqlalchemy import text

from app.core.state import ChainDep
from app.schemas.api import PublicConfig

log = logging.getLogger("wattswap.api")

system_router = APIRouter(tags=["system"])
config_router = APIRouter(tags=["system"])


@system_router.get("/health")
async def health() -> dict[str, str]:
    """Liveness: the process is up."""
    return {"status": "ok"}


@system_router.get("/ready")
async def ready(request: Request) -> JSONResponse:
    """Readiness: Postgres, Redis, the RPC node and the on-chain role wiring."""
    checks: dict[str, str] = {}
    try:
        async with request.app.state.engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        checks["postgres"] = "ok"
    except Exception as exc:
        log.warning("postgres not ready: %s", exc)
        checks["postgres"] = "unavailable"
    try:
        await request.app.state.redis.ping()
        checks["redis"] = "ok"
    except Exception as exc:
        log.warning("redis not ready: %s", exc)
        checks["redis"] = "unavailable"

    chain = request.app.state.chain
    if chain is None:
        checks["chain"] = "not configured"
    else:
        try:
            roles = request.app.state.role_addresses
            problems = await chain.client.verify(roles)
            checks["chain"] = "ok" if not problems else "; ".join(problems)
        except Exception as exc:
            log.warning("chain not ready: %s", exc)
            checks["chain"] = "unavailable"

    ok = all(v == "ok" for v in checks.values())
    return JSONResponse(
        {"status": "ready" if ok else "not_ready", "checks": checks},
        status_code=200 if ok else 503,
    )


@system_router.get("/metrics")
async def metrics() -> Response:
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


@config_router.get("/config", response_model=PublicConfig)
async def public_config(request: Request, chain: ChainDep) -> PublicConfig:
    """Non-secret deployment info for clients and the meter simulator."""
    settings = request.app.state.settings
    params = await chain.client.plausibility()
    return PublicConfig(
        chain_id=settings.chain_id,
        energy_token=chain.client.token_address,
        meter_registry=chain.client.registry_address,
        reading_interval_seconds=settings.reading_interval_seconds,
        min_interval_seconds=params.min_interval_seconds,
        max_interval_seconds=params.max_interval_seconds,
        safety_bps=params.safety_bps,
        note="Testnet demo. Meters are simulated; WATT does not represent physically delivered energy.",
    )
