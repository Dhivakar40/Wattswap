"""END-TO-END PROOF (the CI gate for Phase 2).

SIWE login -> register a meter on-chain -> the real meter-simulator code, in accelerated DEMO
mode, signs EIP-712 readings -> the real API verifies and queues them -> the real mint worker
mints WATT on a real Anvil chain through the real contracts.

Nothing in the chain, database or pipeline is mocked. Only two inputs are fixed so the run is
deterministic and fast: the Open-Meteo call is replaced by the simulator's clear-sky fallback
(`NoWeather`), and the noise is seeded.
"""

import asyncio
import logging
import random
import time
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from eth_account import Account
from meter_sim.client import IngestClient
from meter_sim.device import MeterSpec, SimulatedMeter
from meter_sim.model import GenerationModel, GenerationParams
from meter_sim.runner import RunConfig, run
from meter_sim.weather import NoWeather
from web3 import Web3

from app.models import MintStatus
from tests.conftest import KEY_ALICE, Harness, SettingsFactory
from tests.helpers import INTERVAL, login, mint_rows, new_meter, registry_contract, token_contract

pytestmark = pytest.mark.integration

LAT, LON, CAPACITY_W = 13.0827, 80.2707, 5000  # Chennai, 5 kW rooftop (simulated)
N_INTERVALS = 8


def morning_start() -> int:
    """Yesterday 03:00 UTC (08:30 local at Chennai): the sun is up for the next 2 hours."""
    day = (datetime.now(UTC) - timedelta(days=1)).replace(hour=3, minute=0, second=0, microsecond=0)
    return int(day.timestamp())


def build_sim(h: Harness, meter: dict[str, object], key: str, seed: int = 7) -> SimulatedMeter:
    spec = MeterSpec(str(meter["meter_id"]), key, LAT, LON, CAPACITY_W)
    model = GenerationModel(NoWeather(), GenerationParams(noise_sigma=0.03), random.Random(seed))
    return SimulatedMeter(spec, model, h.settings.chain_id, h.app.state.chain.client.token_address)


async def run_sim(h: Harness, device: SimulatedMeter, start: int, n: int = N_INTERVALS) -> object:
    """Drive the real simulator against the real app, in accelerated DEMO mode."""
    ingest = IngestClient(h.client, backoff_s=0)
    return await run(
        [device],
        ingest,
        RunConfig(
            interval_seconds=INTERVAL,
            start_ts=start,
            demo=True,
            demo_seconds_per_interval=0.05,  # 8 simulated intervals in well under a second
            max_intervals=n,
        ),
        asyncio.Event(),
    )


async def wait_confirmed(h: Harness, expected: int, limit_s: float = 60.0) -> None:
    deadline = time.monotonic() + limit_s
    while time.monotonic() < deadline:
        rows = await mint_rows(h)
        if len(rows) >= expected and all(m.status == MintStatus.CONFIRMED for _, m in rows):
            return
        failed = [m.failure_reason for _, m in rows if m.status == MintStatus.FAILED]
        assert not failed, f"mint failed: {failed}"
        await asyncio.sleep(0.2)
    pytest.fail(f"mints not confirmed in {limit_s}s: {[m.status for _, m in await mint_rows(h)]}")


class _Records(logging.Handler):
    def __init__(self) -> None:
        super().__init__(level=logging.WARNING)
        self.messages: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.messages.append(record.getMessage())


async def test_simulated_meter_mints_watt_on_anvil_end_to_end(
    make_settings: SettingsFactory, web3_sync: Web3
) -> None:
    # The app reconfigures root logging on start-up, so listen on the simulator's own logger.
    records = _Records()
    logging.getLogger("meter_sim.runner").addHandler(records)
    async with Harness(make_settings(mint_worker_enabled=True)) as h:
        # 1. The owner signs in with their wallet (SIWE) and registers a simulated meter.
        alice = await login(h, KEY_ALICE)
        device_key = "0x" + Account.create().key.hex()  # the device generates its own key
        meter, _ = await new_meter(h, alice, device_key, capacity_w=CAPACITY_W, lat=LAT, lon=LON)
        assert meter["register_status"] == "confirmed" and meter["is_simulated"] is True
        registered = registry_contract(h, web3_sync).functions.getMeter(meter["meter_id"]).call()
        assert registered[0] == Account.from_key(KEY_ALICE).address
        assert registered[1] == Account.from_key(device_key).address

        owner = Account.from_key(KEY_ALICE).address
        token = token_contract(h, web3_sync)
        balance_before = token.functions.balanceOf(owner).call()

        # 2. The simulator, in accelerated demo mode, emits signed readings.
        start = morning_start()
        stats = await run_sim(h, build_sim(h, meter, device_key), start)
        assert stats.rejected == 0 and stats.posted == N_INTERVALS  # type: ignore[attr-defined]
        assert any("ACCELERATED DEMO MODE" in m for m in records.messages)  # clearly logged

        # 3. The mint worker mints every reading on-chain.
        await wait_confirmed(h, N_INTERVALS)
        rows = await mint_rows(h)
        total_wh = sum(r.amount_wh for r, _ in rows)
        assert len(rows) == N_INTERVALS and total_wh > 0

        # 4. On-chain truth matches what the simulator produced.
        assert token.functions.balanceOf(owner).call() - balance_before == total_wh
        events = [
            e
            for e in token.events.ReadingMinted().get_logs(from_block=0)
            if "0x" + bytes(e["args"]["meterId"]).hex() == meter["meter_id"]
        ]
        assert len(events) == N_INTERVALS
        assert sorted(e["args"]["amountWh"] for e in events) == sorted(r.amount_wh for r, _ in rows)
        assert [e["args"]["intervalStart"] for e in events] == [
            start + i * INTERVAL for i in range(N_INTERVALS)
        ]  # minted oldest-first
        assert all(m.tx_hash and m.block_number for _, m in rows)
        assert (
            registry_contract(h, web3_sync).functions.lastIntervalEnd(meter["meter_id"]).call()
            == start + N_INTERVALS * INTERVAL
        )
        # Every simulated reading respects the on-chain physical ceiling for its own interval.
        ceiling = registry_contract(h, web3_sync).functions.maxEnergyWh(CAPACITY_W, INTERVAL).call()
        assert all(0 < r.amount_wh <= ceiling for r, _ in rows)

        # 5. Replaying the same readings (the simulator retrying after a crash, or an attacker
        #    re-sending captured traffic) changes nothing: no new rows, no new mint, no new WATT.
        replay = await run_sim(h, build_sim(h, meter, device_key), start)
        assert replay.posted == 0 and replay.duplicates == N_INTERVALS  # type: ignore[attr-defined]
        await asyncio.sleep(1.5)  # give a (non-existent) duplicate mint time to show up
        assert len(await mint_rows(h)) == N_INTERVALS
        assert token.functions.balanceOf(owner).call() - balance_before == total_wh
        assert (
            len(
                [
                    e
                    for e in token.events.ReadingMinted().get_logs(from_block=0)
                    if "0x" + bytes(e["args"]["meterId"]).hex() == meter["meter_id"]
                ]
            )
            == N_INTERVALS
        )

        # 6. The owner sees it through the API.
        view = (await h.client.get(f"/api/v1/meters/{meter['id']}", headers=alice.headers)).json()
        assert view["minted_wh"] == total_wh
        mints = (await h.client.get("/api/v1/mints", headers=alice.headers)).json()["items"]
        assert len(mints) == N_INTERVALS
        assert {m["mint"]["status"] for m in mints} == {"confirmed"}


async def test_night_intervals_produce_no_readings_and_no_mints(
    make_settings: SettingsFactory,
) -> None:
    async with Harness(make_settings(mint_worker_enabled=True)) as h:
        alice = await login(h, KEY_ALICE)
        device_key = "0x" + Account.create().key.hex()
        meter, _ = await new_meter(h, alice, device_key, capacity_w=CAPACITY_W, lat=LAT, lon=LON)
        night = int(
            (datetime.now(UTC) - timedelta(days=1))
            .replace(hour=18, minute=0, second=0, microsecond=0)
            .timestamp()
        )  # 23:30 local
        stats = await run_sim(h, build_sim(h, meter, device_key), night, n=4)
        assert stats.posted == 0 and stats.skipped_no_energy == 4  # type: ignore[attr-defined]
        assert await mint_rows(h) == []


async def test_http_transport_matches_the_asgi_harness(
    make_settings: SettingsFactory, web3_sync: Web3
) -> None:
    """The simulator talks plain HTTP in production; make sure nothing relies on ASGI shortcuts."""
    import uvicorn

    settings = make_settings(mint_worker_enabled=True)
    from app.main import create_app

    app = create_app(settings)
    config = uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning", lifespan="on")
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve())
    try:
        for _ in range(100):
            if server.started:
                break
            await asyncio.sleep(0.1)
        port = server.servers[0].sockets[0].getsockname()[1]
        base = f"http://127.0.0.1:{port}"
        async with httpx.AsyncClient(base_url=base, timeout=30) as http:
            from meter_sim.provision import register_meter, siwe_login

            session = await siwe_login(http, KEY_ALICE, domain="localhost:3000", chain_id=31337)
            device_key = "0x" + Account.create().key.hex()
            meter = await register_meter(
                session,
                label="over-http",
                device_address=Account.from_key(device_key).address,
                capacity_w=CAPACITY_W,
                lat=LAT,
                lon=LON,
            )
            cfg = await IngestClient(http).config()
            spec = MeterSpec(meter["meter_id"], device_key, LAT, LON, CAPACITY_W)
            sim = SimulatedMeter(
                spec,
                GenerationModel(NoWeather(), GenerationParams(), random.Random(3)),
                int(cfg["chain_id"]),
                str(cfg["energy_token"]),
            )
            stats = await run(
                [sim],
                IngestClient(http),
                RunConfig(
                    start_ts=morning_start(),
                    demo=True,
                    demo_seconds_per_interval=0,
                    max_intervals=3,
                ),
                asyncio.Event(),
            )
            assert stats.posted == 3 and stats.rejected == 0
            for _ in range(100):
                items = (await http.get("/api/v1/mints", headers=session.headers)).json()["items"]
                if len(items) == 3 and all(i["mint"]["status"] == "confirmed" for i in items):
                    break
                await asyncio.sleep(0.2)
            else:
                pytest.fail("mints did not confirm over HTTP")
            minted = token_contract_balance(web3_sync, cfg["energy_token"], session.address)
            assert minted >= sum(i["amount_wh"] for i in items)
    finally:
        server.should_exit = True
        await task


def token_contract_balance(w3: Web3, token_address: str, owner: str) -> int:
    abi = [
        {
            "type": "function",
            "name": "balanceOf",
            "stateMutability": "view",
            "inputs": [{"name": "a", "type": "address"}],
            "outputs": [{"name": "", "type": "uint256"}],
        }
    ]
    return int(
        w3.eth.contract(address=Web3.to_checksum_address(token_address), abi=abi)
        .functions.balanceOf(owner)
        .call()
    )
