"""Shared fixtures.

Tests run against REAL Postgres, Redis and Anvil (no mocks of the chain or database):

* Postgres + Redis: the docker-compose services (`make up`) or CI service containers.
* Anvil: spawned per test session on a free port, with the real `Deploy.s.sol` script run
  against it, so the deployed role wiring is exactly what production would get.

If a service is missing the dependent tests are skipped, unless REQUIRE_SERVICES=1 (CI sets
this), in which case they fail loudly instead of passing vacuously.
"""

import asyncio
import json
import os
import shutil
import socket
import subprocess
import time
from collections.abc import AsyncIterator, Callable, Iterator
from pathlib import Path
from typing import Any

import asyncpg
import httpx
import pytest
import redis.asyncio as aioredis
from alembic.config import Config
from eth_account import Account
from pydantic import SecretStr
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine
from web3 import Web3

from alembic import command
from app.core.config import Settings
from app.main import create_app

API_DIR = Path(__file__).resolve().parents[1]
REPO = API_DIR.parents[1]
CONTRACTS = REPO / "contracts"

# Public Anvil dev keys. Worthless anywhere but a local chain.
KEY_DEPLOYER = "0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80"  # #0
KEY_RELAYER = "0x59c6995e998f97a5a0044966f0945389dc9e86dae88c7a8412f4603b6b78690d"  # #1
KEY_ORACLE = "0x5de4111afa1a4b94908f83103eb1f1706367c2e68ca870fc3fb9a804cdab365a"  # #2
KEY_PRICER = "0x7c852118294e51e653712a81e05800f419141751be58f605c371e15141b007a6"  # #3
KEY_REGISTRAR = "0x47e179ec197488593b187f80a00eb0da91f1b9d0b13f8733639f19c30a34926a"  # #4
KEY_ALICE = "0x8b3a350cf5c34c9194ca85829a2df0ec3153be0318b5e2d3348e872092edffba"  # #5
KEY_BOB = "0x92db14e403b83dfe3df233f83dfa3a0d7096f21ca9b0d6d6b8d88b2b4ec1564e"  # #6

SIWE_DOMAIN = "localhost:3000"
REQUIRE_SERVICES = os.environ.get("REQUIRE_SERVICES") == "1"


def _unavailable(reason: str) -> None:
    if REQUIRE_SERVICES:
        pytest.fail(f"required service unavailable: {reason}")
    pytest.skip(reason)


def _tool(name: str) -> str | None:
    found = shutil.which(name)
    if found:
        return found
    local = Path.home() / ".foundry" / "bin" / (name + (".exe" if os.name == "nt" else ""))
    return str(local) if local.exists() else None


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


# ---------------------------------------------------------------------------- Postgres/Redis
@pytest.fixture(scope="session")
def database_url() -> str:
    base = os.environ.get(
        "DATABASE_URL",
        "postgresql+asyncpg://wattswap:wattswap_dev_password@localhost:5432/wattswap",
    )
    url = make_url(base)
    return url.set(database="wattswap_test").render_as_string(hide_password=False)


@pytest.fixture(scope="session")
def redis_url() -> str:
    base = os.environ.get("REDIS_URL", "redis://localhost:6379/0")
    return base.rsplit("/", 1)[0] + "/15"  # dedicated logical DB; flushed between tests


@pytest.fixture(scope="session")
def migrated_db(database_url: str) -> str:
    """Create the test database (if needed) and run the real Alembic migrations on it."""
    url = make_url(database_url)
    admin_dsn = url.set(drivername="postgresql", database="postgres").render_as_string(
        hide_password=False
    )

    async def ensure() -> None:
        conn = await asyncpg.connect(admin_dsn, timeout=5)
        try:
            exists = await conn.fetchval("SELECT 1 FROM pg_database WHERE datname=$1", url.database)
            if not exists:
                await conn.execute(f'CREATE DATABASE "{url.database}"')
        finally:
            await conn.close()

    try:
        asyncio.run(ensure())
    except (OSError, asyncpg.PostgresError, TimeoutError) as exc:
        _unavailable(f"postgres: {exc}")
    cfg = Config(str(API_DIR / "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", database_url)
    command.upgrade(cfg, "head")
    return database_url


@pytest.fixture(scope="session")
def redis_ok(redis_url: str) -> str:
    async def ping() -> None:
        r = aioredis.from_url(redis_url)
        try:
            await r.ping()
        finally:
            await r.aclose()

    try:
        asyncio.run(ping())
    except Exception as exc:
        _unavailable(f"redis: {exc}")
    return redis_url


# ---------------------------------------------------------------------------------- Anvil
@pytest.fixture(scope="session")
def chain() -> Iterator[dict[str, str]]:
    """A fresh Anvil with the real deploy script applied. Yields rpc url + addresses."""
    anvil = _tool("anvil")
    forge = _tool("forge")
    if not anvil or not forge:
        _unavailable("foundry (anvil/forge) not installed")
    assert anvil and forge
    port = _free_port()
    rpc = f"http://127.0.0.1:{port}"
    proc = subprocess.Popen(  # noqa: S603
        [anvil, "--port", str(port), "--chain-id", "31337", "--silent"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        w3 = Web3(Web3.HTTPProvider(rpc))
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            try:
                if w3.is_connected():
                    break
            except Exception:  # noqa: S110 - still starting up
                pass
            time.sleep(0.2)
        else:
            pytest.fail("anvil did not start")

        env = {
            **os.environ,
            "PRIVATE_KEY": KEY_DEPLOYER,
            "RELAYER_ADDRESS": Account.from_key(KEY_RELAYER).address,
            "MINTER_ADDRESS": Account.from_key(KEY_ORACLE).address,
            "PRICER_ADDRESS": Account.from_key(KEY_PRICER).address,
            "REGISTRAR_ADDRESS": Account.from_key(KEY_REGISTRAR).address,
        }
        result = subprocess.run(  # noqa: S603
            [forge, "script", "script/Deploy.s.sol", "--rpc-url", rpc, "--broadcast"],
            cwd=CONTRACTS,
            env=env,
            capture_output=True,
            text=True,
            timeout=300,
            check=False,
        )
        if result.returncode != 0:
            pytest.fail(f"forge deploy failed:\n{result.stdout}\n{result.stderr}")
        run = json.loads(
            (CONTRACTS / "broadcast" / "Deploy.s.sol" / "31337" / "run-latest.json").read_text()
        )
        addresses = {
            tx["contractName"]: Web3.to_checksum_address(tx["contractAddress"])
            for tx in run["transactions"]
            if tx.get("transactionType") == "CREATE"
        }
        yield {"rpc": rpc, **addresses}
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()


# ---------------------------------------------------------------------------------- app
SettingsFactory = Callable[..., Settings]


@pytest.fixture
def make_settings(migrated_db: str, redis_ok: str, chain: dict[str, str]) -> SettingsFactory:
    def factory(**overrides: Any) -> Settings:
        values: dict[str, Any] = {
            "api_env": "test",
            "database_url": migrated_db,
            "redis_url": redis_ok,
            "rpc_url": chain["rpc"],
            "chain_id": 31337,
            "jwt_secret": SecretStr("test-secret-test-secret-test-secret-0123456789"),
            "siwe_domain": SIWE_DOMAIN,
            "oracle_private_key": SecretStr(KEY_ORACLE),
            "registrar_private_key": SecretStr(KEY_REGISTRAR),
            "energy_token_address": chain["EnergyToken"],
            "meter_registry_address": chain["MeterRegistry"],
            "mint_worker_enabled": False,  # tests drive the worker explicitly unless they opt in
            "mint_poll_seconds": 0.2,
            "mint_tx_timeout_seconds": 20.0,
            "rate_limit_ip": 10_000,
            "rate_limit_auth": 10_000,
            "rate_limit_user": 10_000,
            "rate_limit_meter_ingest": 10_000,
        }
        values.update(overrides)
        return Settings(_env_file=None, **values)

    return factory


@pytest.fixture(autouse=True)
async def clean_state(request: pytest.FixtureRequest) -> AsyncIterator[None]:
    """Empty every table and the Redis test DB before each test that uses the app."""
    if "make_settings" not in request.fixturenames:
        yield
        return
    db_url: str = request.getfixturevalue("migrated_db")
    redis_url: str = request.getfixturevalue("redis_ok")
    engine = create_async_engine(db_url)
    async with engine.begin() as conn:
        tables = (
            await conn.execute(
                text(
                    "SELECT tablename FROM pg_tables WHERE schemaname='public' "
                    "AND tablename <> 'alembic_version'"
                )
            )
        ).scalars()
        names = ", ".join(f'"{t}"' for t in tables)
        if names:
            await conn.execute(text(f"TRUNCATE {names} RESTART IDENTITY CASCADE"))
    await engine.dispose()
    r = aioredis.from_url(redis_url)
    await r.flushdb()
    await r.aclose()
    yield


class Harness:
    """A running app (lifespan started) plus an HTTP client that sends Secure cookies."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.app = create_app(settings)

    async def __aenter__(self) -> "Harness":
        self._lifespan = self.app.router.lifespan_context(self.app)
        await self._lifespan.__aenter__()
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.app), base_url="https://localhost"
        )
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.client.aclose()
        await self._lifespan.__aexit__(None, None, None)


HarnessFactory = Callable[..., Harness]


@pytest.fixture
async def harness(make_settings: SettingsFactory) -> AsyncIterator[Harness]:
    async with Harness(make_settings()) as h:
        yield h


@pytest.fixture
def web3_sync(chain: dict[str, str]) -> Web3:
    return Web3(Web3.HTTPProvider(chain["rpc"]))
