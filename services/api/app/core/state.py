"""Typed accessors for objects the lifespan stores on `app.state`."""

from dataclasses import dataclass
from typing import Annotated

import redis.asyncio as aioredis
from fastapi import Depends, Request

from app.chain.client import ChainClient
from app.chain.tx import TxSender
from app.core.config import Settings
from app.core.errors import AppError


@dataclass
class ChainServices:
    client: ChainClient
    oracle: TxSender | None  # signs EnergyToken.mint
    registrar: TxSender | None  # signs MeterRegistry admin calls


def get_settings_dep(request: Request) -> Settings:
    settings: Settings = request.app.state.settings
    return settings


def get_redis(request: Request) -> aioredis.Redis:
    redis: aioredis.Redis = request.app.state.redis
    return redis


def get_chain(request: Request) -> ChainServices:
    chain: ChainServices | None = request.app.state.chain
    if chain is None:
        raise AppError(503, "chain_unavailable", "blockchain connection is not configured")
    return chain


SettingsDep = Annotated[Settings, Depends(get_settings_dep)]
RedisDep = Annotated[aioredis.Redis, Depends(get_redis)]
ChainDep = Annotated[ChainServices, Depends(get_chain)]
