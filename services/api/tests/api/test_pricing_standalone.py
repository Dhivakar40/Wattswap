"""The pricing engine as its own process: `python -m app.pricing.run`."""

import asyncio
import json
import os
import subprocess
import sys
import time

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import create_async_engine

from app.models import PriceTick
from app.pricing import publish
from app.pricing.repo import DEFAULT_MARKET_ID
from tests.conftest import API_DIR, KEY_ORACLE, KEY_PRICER, KEY_REGISTRAR, Harness, SettingsFactory


async def test_the_engine_runs_as_a_standalone_process(make_settings: SettingsFactory) -> None:
    async with Harness(make_settings()) as h:
        s = h.settings
        env = {
            **os.environ,
            "API_ENV": "test",
            "DATABASE_URL": s.database_url,
            "REDIS_URL": s.redis_url,
            "RPC_URL": s.rpc_url,
            "CHAIN_ID": str(s.chain_id),
            "ENERGY_TOKEN_ADDRESS": h.app.state.chain.client.token_address,
            "METER_REGISTRY_ADDRESS": h.app.state.chain.client.registry_address,
            "EXCHANGE_ADDRESS": h.app.state.chain.client.exchange_address,
            "PRICE_ORACLE_ADDRESS": h.app.state.chain.client.oracle_address,
            "PRICER_PRIVATE_KEY": KEY_PRICER,
            "ORACLE_PRIVATE_KEY": KEY_ORACLE,
            "REGISTRAR_PRIVATE_KEY": KEY_REGISTRAR,
            "PRICING_EMBEDDED": "false",
            "OPEN_METEO_FORECAST_URL": "http://127.0.0.1:9/forecast",  # unreachable: no network in tests
            "LOG_LEVEL": "WARNING",
        }
        proc = subprocess.Popen(  # noqa: ASYNC220 - a short-lived child we poll below
            [sys.executable, "-m", "app.pricing.run"],
            cwd=API_DIR,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        engine = create_async_engine(s.database_url)
        try:
            deadline = time.monotonic() + 70
            count = 0
            while (
                time.monotonic() < deadline and count < 2
            ):  # first tick at once, the next after 15 s
                await asyncio.sleep(1)
                assert proc.poll() is None, (
                    f"process exited early:\n{proc.stdout.read() if proc.stdout else ''}"
                )
                async with engine.connect() as conn:
                    count = int(await conn.scalar(select(func.count()).select_from(PriceTick)) or 0)
            if count < 2:
                proc.terminate()
                out = proc.communicate(timeout=15)[0]
                raise AssertionError(
                    f"the standalone engine produced {count} ticks. Output:\n{out[:2500]}"
                )

            async with h.app.state.sessionmaker() as session:
                row = await session.scalar(select(PriceTick).order_by(PriceTick.ts.desc()).limit(1))
            assert row is not None and row.market_id == DEFAULT_MARKET_ID
            assert row.signature and row.signature.startswith(
                "0x"
            )  # it signed quotes with the pricer key
            assert row.price == 150_000  # an empty market prices at the neutral base
            latest = json.loads(await h.app.state.redis.get(publish.latest_key(DEFAULT_MARKET_ID)))
            assert latest["seq"] >= 2 and latest["data"]["quote"]["price"] == 150_000
            assert await h.app.state.redis.get(publish.state_key(DEFAULT_MARKET_ID))
        finally:
            await engine.dispose()
            proc.terminate()
            try:
                proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                proc.kill()
