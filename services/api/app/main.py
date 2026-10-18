"""FastAPI application entrypoint."""

import logging
import uuid
from collections.abc import Awaitable, Callable

import redis.asyncio as aioredis
from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from app.core.config import get_settings
from app.core.logging import configure_logging, request_id_ctx

log = logging.getLogger("wattswap.api")


def create_app() -> FastAPI:
    settings = get_settings()
    configure_logging(settings.api_log_level)
    app = FastAPI(title="WattSwap API", version="0.0.0", docs_url="/docs")

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
        allow_headers=["*"],
    )

    @app.middleware("http")
    async def request_id_middleware(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        rid = request.headers.get("x-request-id") or uuid.uuid4().hex
        token = request_id_ctx.set(rid)
        try:
            response = await call_next(request)
        finally:
            request_id_ctx.reset(token)
        response.headers["x-request-id"] = rid
        return response

    @app.get("/health")
    async def health() -> dict[str, str]:
        """Liveness: the process is up."""
        return {"status": "ok"}

    @app.get("/ready")
    async def ready() -> JSONResponse:
        """Readiness: Postgres and Redis are reachable."""
        checks: dict[str, str] = {}
        engine = create_async_engine(settings.database_url)
        try:
            async with engine.connect() as conn:
                await conn.execute(text("SELECT 1"))
            checks["postgres"] = "ok"
        except Exception as exc:
            log.warning("postgres not ready: %s", exc)
            checks["postgres"] = "unavailable"
        finally:
            await engine.dispose()

        client = aioredis.from_url(settings.redis_url)
        try:
            await client.ping()
            checks["redis"] = "ok"
        except Exception as exc:
            log.warning("redis not ready: %s", exc)
            checks["redis"] = "unavailable"
        finally:
            await client.aclose()

        ok = all(v == "ok" for v in checks.values())
        return JSONResponse(
            {"status": "ready" if ok else "not_ready", "checks": checks},
            status_code=200 if ok else 503,
        )

    @app.get("/metrics")
    async def metrics() -> Response:
        return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)

    return app


app = create_app()
