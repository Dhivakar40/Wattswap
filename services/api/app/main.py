"""FastAPI application entrypoint."""

import asyncio
import contextlib
import logging
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager

import redis.asyncio as aioredis
from fastapi import APIRouter, FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware

from app.chain.client import ChainClient
from app.chain.tx import TxSender
from app.core.config import ConfigError, Settings, assert_distinct_role_keys, get_settings
from app.core.db import make_engine, make_sessionmaker
from app.core.errors import install_error_handlers
from app.core.logging import configure_logging, request_id_ctx
from app.core.metrics import HTTP_LATENCY, HTTP_REQUESTS
from app.core.state import ChainServices
from app.pricing.factory import PricingBundle, build_pricing
from app.realtime import channels as rt_channels
from app.realtime.gateway import Gateway
from app.realtime.snapshot import EPOCH_HEADER, SEQ_HEADER
from app.routers import (
    admin_pricing,
    admin_trading,
    auth,
    markets,
    meters,
    orders,
    readings,
    system,
    ws,
)
from app.workers.mint_worker import MintWorker
from app.workers.trading import TradingBundle, build_trading, validate

log = logging.getLogger("wattswap.api")


async def _build_chain(settings: Settings, redis: aioredis.Redis) -> ChainServices | None:
    try:
        client = ChainClient(settings)
    except (ConfigError, FileNotFoundError) as exc:
        if settings.api_env == "production":
            raise
        log.warning("chain disabled: %s", exc)
        return None

    def sender(secret: object, name: str) -> TxSender | None:
        from pydantic import SecretStr

        if not isinstance(secret, SecretStr):
            return None
        return TxSender(client, client.account(secret.get_secret_value()), redis, name)

    return ChainServices(
        client=client,
        oracle=sender(settings.oracle_private_key, "oracle"),
        registrar=sender(settings.registrar_private_key, "registrar"),
    )


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings.api_log_level)
    assert_distinct_role_keys(settings)  # refuse to boot on shared role keys

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        engine = make_engine(settings.database_url)
        redis = aioredis.from_url(settings.redis_url, decode_responses=True)
        app.state.settings = settings
        app.state.engine = engine
        app.state.sessionmaker = make_sessionmaker(engine)
        app.state.redis = redis
        app.state.role_addresses = settings.role_key_addresses()
        chain = await _build_chain(settings, redis)
        app.state.chain = chain
        rt_channels.configure_retention(rt_channels.retention_from_settings(settings))
        gateway = Gateway(settings, redis, app.state.sessionmaker)
        await gateway.start()
        app.state.gateway = gateway

        stop = asyncio.Event()
        worker_task: asyncio.Task[None] | None = None
        if chain is not None:
            try:
                problems = await chain.client.verify(app.state.role_addresses)
            except Exception as exc:
                problems = [f"chain unreachable: {exc}"]
            for problem in problems:
                log.error("chain check failed: %s", problem)
            if problems and settings.api_env == "production":
                raise ConfigError("; ".join(problems))
            if settings.mint_worker_enabled and chain.oracle is not None and not problems:
                worker = MintWorker(
                    settings, app.state.sessionmaker, redis, chain.client, chain.oracle
                )
                worker_task = asyncio.create_task(worker.run(stop), name="mint-worker")
                app.state.mint_worker = worker
            elif settings.mint_worker_enabled:
                log.warning("mint worker not started (missing oracle key or chain problems)")
        pricing: PricingBundle | None = None
        pricing_task: asyncio.Task[None] | None = None
        if settings.pricing_enabled and settings.pricing_embedded:
            pricing = build_pricing(
                settings, app.state.sessionmaker, redis, None if chain is None else chain.client
            )
            app.state.pricing = pricing.service
            pricing_task = asyncio.create_task(pricing.service.run(stop), name="pricing")
        trading: TradingBundle | None = None
        trading_task: asyncio.Task[None] | None = None
        if (
            settings.trading_enabled
            and settings.trading_embedded
            and chain is not None
            and settings.relayer_private_key is not None
        ):
            trading = build_trading(settings, app.state.sessionmaker, redis, chain.client)
            trading_problems = await validate(
                settings, chain.client, trading.relayer.sender.address
            )
            for problem in trading_problems:
                log.error("trading config check failed: %s", problem)
            if trading_problems and settings.api_env == "production":
                raise ConfigError("; ".join(trading_problems))
            if not trading_problems:
                trading_task = asyncio.create_task(trading.run(stop), name="trading")
            app.state.trading = trading
        elif settings.trading_enabled and settings.trading_embedded:
            log.warning("trading workers not started (no chain or RELAYER_PRIVATE_KEY)")
        try:
            yield
        finally:
            stop.set()
            await gateway.stop()
            for task in (worker_task, pricing_task, trading_task):
                if task is not None:
                    with contextlib.suppress(asyncio.CancelledError, Exception):
                        await asyncio.wait_for(task, timeout=10)
            if pricing is not None:
                await pricing.aclose()
            if chain is not None:
                await chain.client.close()
            await redis.aclose()
            await engine.dispose()

    app = FastAPI(title="WattSwap API", version="0.2.0", docs_url="/docs", lifespan=lifespan)

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["content-type", "x-request-id"],
        expose_headers=["x-request-id", "retry-after", SEQ_HEADER, EPOCH_HEADER],
    )

    @app.middleware("http")
    async def request_context(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        rid = request.headers.get("x-request-id") or uuid.uuid4().hex
        token = request_id_ctx.set(rid)
        started = time.perf_counter()
        status = 500
        try:
            response = await call_next(request)
            status = response.status_code
        finally:
            request_id_ctx.reset(token)
            route = request.scope.get("route")
            path = getattr(route, "path", "unmatched")
            HTTP_REQUESTS.labels(request.method, path, str(status)).inc()
            HTTP_LATENCY.labels(request.method, path).observe(time.perf_counter() - started)
        response.headers["x-request-id"] = rid
        return response

    install_error_handlers(app)

    api_v1 = APIRouter(prefix="/api/v1")
    api_v1.include_router(auth.router)
    api_v1.include_router(meters.router)
    api_v1.include_router(readings.router)
    api_v1.include_router(markets.router)
    api_v1.include_router(orders.router)
    api_v1.include_router(admin_pricing.router)
    api_v1.include_router(admin_trading.router)
    api_v1.include_router(system.config_router)
    app.include_router(system.system_router)
    app.include_router(ws.router)
    app.include_router(api_v1)
    return app
