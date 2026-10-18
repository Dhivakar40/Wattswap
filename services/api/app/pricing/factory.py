"""Builds a PricingService from settings. Shared by the API lifespan and the standalone CLI."""

import random
import time
from dataclasses import dataclass

import httpx
import redis.asyncio as aioredis
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.chain.client import ChainClient
from app.chain.tx import TxSender
from app.core.config import ConfigError, Settings
from app.pricing.service import PricingService
from app.pricing.weather import OpenMeteo, WeatherService


@dataclass
class PricingBundle:
    service: PricingService
    http: httpx.AsyncClient

    async def aclose(self) -> None:
        await self.http.aclose()


def build_pricing(
    settings: Settings,
    sessionmaker: async_sessionmaker[AsyncSession],
    redis: aioredis.Redis,
    chain: ChainClient | None,
) -> PricingBundle:
    http = httpx.AsyncClient()
    provider = OpenMeteo(
        http,
        forecast_url=settings.open_meteo_forecast_url,
        archive_url=settings.open_meteo_archive_url,
        timeout_s=settings.open_meteo_timeout_seconds,
    )
    weather = WeatherService(provider, clock=time.time, rng=random.Random())  # noqa: S311
    pricer: TxSender | None = None
    key = settings.pricer_private_key
    if key is not None and chain is not None:
        pricer = TxSender(chain, chain.account(key.get_secret_value()), redis, "pricer")
    elif settings.api_env == "production":
        raise ConfigError("PRICER_PRIVATE_KEY and a chain connection are required in production")
    return PricingBundle(
        PricingService(settings, sessionmaker, redis, weather, chain=chain, pricer=pricer), http
    )
