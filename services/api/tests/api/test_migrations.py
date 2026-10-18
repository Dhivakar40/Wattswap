"""Alembic migrations: apply, roll back, re-apply, and match the ORM models exactly."""

import asyncio
from pathlib import Path

import asyncpg
import pytest
from alembic.config import Config
from sqlalchemy.engine import make_url

from alembic import command
from tests.conftest import API_DIR

ENUM_TYPES = {
    "user_role",
    "register_status",
    "mint_status",
    "order_side",
    "order_status",
    "settlement_status",
}
EXPECTED_TABLES = {
    "users",
    "meters",
    "readings",
    "mints",
    "orders",
    "trades",
    "price_ticks",
    "settlements",
    "markets",
    "pricing_params",
    "pricing_param_changes",
    "pricing_events",
}


def _cfg(url: str) -> Config:
    cfg = Config(str(Path(API_DIR) / "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", url)
    return cfg


async def _tables(dsn: str) -> set[str]:
    conn = await asyncpg.connect(dsn)
    try:
        rows = await conn.fetch(
            "SELECT tablename FROM pg_tables WHERE schemaname='public' AND tablename <> 'alembic_version'"
        )
        types = await conn.fetch(
            "SELECT typname FROM pg_type WHERE typname = ANY($1::text[])", sorted(ENUM_TYPES)
        )
        return {r["tablename"] for r in rows} | {f"type:{t['typname']}" for t in types}
    finally:
        await conn.close()


@pytest.fixture
def scratch_db(migrated_db: str) -> str:
    """A throwaway database so up/down/up never disturbs the shared test database."""
    url = make_url(migrated_db)
    admin = url.set(drivername="postgresql", database="postgres").render_as_string(
        hide_password=False
    )
    name = "wattswap_migrations_scratch"

    async def recreate() -> None:
        conn = await asyncpg.connect(admin)
        try:
            await conn.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
            await conn.execute(f'CREATE DATABASE "{name}"')
        finally:
            await conn.close()

    asyncio.run(recreate())
    return url.set(database=name).render_as_string(hide_password=False)


def test_up_down_up_and_no_model_drift(scratch_db: str) -> None:
    plain = scratch_db.replace("+asyncpg", "")
    cfg = _cfg(scratch_db)

    command.upgrade(cfg, "head")
    after_up = asyncio.run(_tables(plain))
    assert EXPECTED_TABLES <= after_up
    assert {"type:mint_status", "type:user_role"} <= after_up

    command.check(cfg)  # raises if the ORM models and the migrations disagree

    command.downgrade(cfg, "base")
    assert asyncio.run(_tables(plain)) == set()  # tables AND enum types are gone

    command.upgrade(cfg, "head")
    assert EXPECTED_TABLES <= asyncio.run(_tables(plain))
    command.check(cfg)
