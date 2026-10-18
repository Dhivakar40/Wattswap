"""Test helpers shared by the API and integration suites."""

import time
from typing import Any

from eth_account import Account
from meter_sim.provision import Session, register_meter, siwe_login
from meter_sim.signing import SignedReading, sign_reading
from sqlalchemy import select
from web3 import Web3

from app.models import Mint, Reading
from app.workers.mint_worker import MintWorker
from tests.conftest import SIWE_DOMAIN, Harness


async def login(h: Harness, key: str) -> Session:
    return await siwe_login(h.client, key, domain=SIWE_DOMAIN, chain_id=h.settings.chain_id)


async def new_meter(
    h: Harness,
    session: Session,
    device_key: str | None = None,
    *,
    capacity_w: int = 5000,
    lat: float = 13.0827,
    lon: float = 80.2707,
) -> tuple[dict[str, Any], str]:
    """Register a meter through the public API; returns (meter json, device private key)."""
    device_key = device_key or ("0x" + Account.create().key.hex())
    meter = await register_meter(
        session,
        label="test-meter",
        device_address=Account.from_key(device_key).address,
        capacity_w=capacity_w,
        lat=lat,
        lon=lon,
    )
    return meter, device_key


# ------------------------------------------------------------------------------ readings
INTERVAL = 900


def past_start(intervals_ago: int = 40, interval: int = INTERVAL) -> int:
    """An interval boundary safely in the past (and aligned), `intervals_ago` slots back."""
    return int(time.time()) // interval * interval - intervals_ago * interval


def signed_reading(
    h: Harness,
    meter: dict[str, Any],
    device_key: str,
    start: int,
    wh: int = 500,
    seconds: int = INTERVAL,
) -> SignedReading:
    chain = h.app.state.chain.client
    return sign_reading(
        device_key, h.settings.chain_id, chain.token_address, meter["meter_id"], wh, start, seconds
    )


async def post_reading(h: Harness, r: SignedReading) -> Any:
    return await h.client.post("/api/v1/readings", json=r.to_json())


def make_worker(h: Harness) -> MintWorker:
    chain = h.app.state.chain
    assert chain.oracle is not None
    return MintWorker(
        h.settings, h.app.state.sessionmaker, h.app.state.redis, chain.client, chain.oracle
    )


async def mint_rows(h: Harness) -> list[tuple[Reading, Mint]]:
    async with h.app.state.sessionmaker() as s:
        rows = (
            await s.execute(
                select(Reading, Mint)
                .join(Mint, Mint.reading_pk == Reading.id)
                .order_by(Reading.interval_start)
            )
        ).all()
    return [(r, m) for r, m in rows]


def token_contract(h: Harness, w3: Web3) -> Any:
    return w3.eth.contract(
        address=h.app.state.chain.client.token_address, abi=h.settings.load_abi("EnergyToken")
    )


def registry_contract(h: Harness, w3: Web3) -> Any:
    return w3.eth.contract(
        address=h.app.state.chain.client.registry_address, abi=h.settings.load_abi("MeterRegistry")
    )
