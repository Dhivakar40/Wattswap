"""END-TO-END (CI gate): the accelerated simulator drives the engine's clock and the supply.

A simulated meter runs in DEMO mode (one 15-minute interval per fraction of a second). The
mint worker turns its readings into WATT, the pricing engine follows the simulated clock for
weather and the load profile, and the price moves through the simulated morning. Open-Meteo is
mocked (a hand-specified clear-sky day); everything else is real: Postgres, Redis, Anvil, the
contracts, the pipeline and the engine.
"""

import asyncio
import json
import random
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from itertools import pairwise

import httpx
import pytest
import respx
from eth_account import Account
from meter_sim.client import IngestClient
from meter_sim.device import MeterSpec, SimulatedMeter
from meter_sim.model import GenerationModel, GenerationParams
from meter_sim.runner import RunConfig, run
from meter_sim.weather import NoWeather

from app.models import MintStatus
from app.pricing import publish
from app.pricing.engine import Tick
from app.pricing.repo import DEFAULT_MARKET_ID
from app.pricing.trace import CLEAR_SKY_GHI
from tests.api.pricing_helpers import FORECAST, FakeClock, make_service
from tests.conftest import Harness, SettingsFactory
from tests.helpers import INTERVAL, login, mint_rows, new_meter

pytestmark = pytest.mark.integration
MID = DEFAULT_MARKET_ID
LAT, LON, CAPACITY_W = 13.0827, 80.2707, 5000


def clear_sky_day(day_start: int) -> dict[str, object]:
    # CLEAR_SKY_GHI is indexed by local solar hour; Chennai is ~5 h ahead of UTC, so shift it.
    ghi = [CLEAR_SKY_GHI[(h + 5) % 24] for h in range(24)]
    return {
        "hourly": {
            "time": [day_start + 3600 * i for i in range(24)],
            "shortwave_radiation": ghi,
            "temperature_2m": [22 + 8 * g / 900 for g in ghi],
            "cloud_cover": [10] * 24,
        }
    }


@respx.mock
async def test_prices_move_while_the_simulator_runs_accelerated(
    make_settings: SettingsFactory,
) -> None:
    def answer(request: httpx.Request) -> httpx.Response:
        if "start_date" in request.url.params:  # one UTC day (the demo clock's history path)
            day = datetime.fromisoformat(request.url.params["start_date"]).replace(tzinfo=UTC)
            return httpx.Response(200, json=clear_sky_day(int(day.timestamp())))
        n = len(request.url.params["latitude"].split(","))  # the live batched poll: 3 days
        today = int(
            datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
        )
        days = [clear_sky_day(today + d * 86_400) for d in (-1, 0, 1)]
        merged = {
            "hourly": {
                k: [v for d in days for v in d["hourly"][k]]  # type: ignore[index]
                for k in ("time", "shortwave_radiation", "temperature_2m", "cloud_cover")
            }
        }
        return httpx.Response(200, json=[merged] * n if n > 1 else merged)

    respx.get(FORECAST).mock(side_effect=answer)

    async with Harness(
        make_settings(
            mint_worker_enabled=True,
            pricing_clock_mode="follow_readings",
            pricing_inventory_refresh_seconds=1,
            clock_follow_window_seconds=600,
        )
    ) as h:
        alice = await login(h, "0x" + Account.create().key.hex())
        device_key = "0x" + Account.create().key.hex()
        meter, _ = await new_meter(h, alice, device_key, capacity_w=CAPACITY_W, lat=LAT, lon=LON)

        clock = FakeClock()
        svc = make_service(h, clock)
        assert await svc.poll_weather_once() == "ok"  # live weather, as a running system has

        # --- the simulator, accelerated: yesterday 03:00 UTC (08:30 in Chennai) onward -----------
        start = int(
            (datetime.now(UTC) - timedelta(days=1))
            .replace(hour=3, minute=0, second=0, microsecond=0)
            .timestamp()
        )
        spec = MeterSpec(meter["meter_id"], device_key, LAT, LON, CAPACITY_W)
        model = GenerationModel(NoWeather(), GenerationParams(noise_sigma=0.03), random.Random(11))
        sim = SimulatedMeter(
            spec, model, h.settings.chain_id, h.app.state.chain.client.token_address
        )
        intervals = 10

        async def simulator() -> None:
            stats = await run(
                [sim],
                IngestClient(h.client, backoff_s=0),
                RunConfig(
                    start_ts=start,
                    demo=True,
                    demo_seconds_per_interval=0.6,
                    max_intervals=intervals,
                ),
                asyncio.Event(),
            )
            assert stats.posted == intervals and stats.rejected == 0

        # --- the engine ticks while it runs (one tick per 15 s of fake wall time) ---------------
        ticks: list[Tick] = []

        async def engine() -> None:
            while (len(ticks) < 60 and not sim_task.done()) or len(ticks) < 4:
                clock.advance(15)
                ticks.extend(await svc.tick_all())
                await asyncio.sleep(0.12)

        sim_task = asyncio.create_task(simulator())
        await engine()
        await sim_task
        # let the last mints land, then tick a few more times so the inventory term catches up
        for _ in range(100):
            rows = await mint_rows(h)
            if len(rows) == intervals and all(m.status == MintStatus.CONFIRMED for _, m in rows):
                break
            await asyncio.sleep(0.3)
        for _ in range(3):
            clock.advance(15)
            ticks.extend(await svc.tick_all())

        followed = [t for t in ticks if t.drivers["market_time"] < t.ts - 3600]
        assert len(followed) >= 8, "the engine did not follow the simulated clock"
        times = [t.market_time for t in followed]
        assert (
            times == sorted(times)
            and times[0] >= start + INTERVAL
            and times[-1] <= start + intervals * INTERVAL
        )
        assert len(set(times)) >= 5  # market time swept through the simulated morning

        # weather and supply follow the simulated sun: GHI rises through the morning
        ghis = [Decimal(t.drivers["weather"][0]["ghi_wm2"]) for t in followed]
        assert ghis[-1] > ghis[0] and all(
            t.drivers["weather"][0]["source"] in ("live", "archive") for t in followed
        )
        gens = [Decimal(t.drivers["supply"]["generation_kw"]) for t in followed]
        assert gens[-1] > gens[0] > 0

        # the minted WATT became tradeable inventory, which entered the supply term
        assert ticks[0].drivers["supply"]["inventory_wh"] == 0
        assert ticks[-1].drivers["supply"]["inventory_wh"] > 0
        assert ticks[-1].drivers["supply"]["inventory_source"] == "held_balances"

        # so the price falls through the morning, rate-limited and without any anomaly freeze
        assert ticks[-1].raw_price < ticks[0].raw_price
        assert ticks[-1].price < ticks[0].price
        assert len({t.price for t in ticks}) > 5, "the price did not move during the demo"
        assert all(t.breaker.mode.value != "FROZEN" for t in ticks)
        for a, b in pairwise(ticks):
            assert abs(b.price - a.price) * 10_000 <= a.price * 25

        # quotes stayed on the real (chain) clock throughout, and the Redis feed shows the demo
        wire = json.loads(await h.app.state.redis.get(publish.latest_key(MID)))["data"]
        assert wire["quote"]["timestamp"] >= int(clock.t) - 120
        assert wire["accelerated"] is True or ticks[-1].market_time >= int(clock.t) - 60
        assert wire["clockMode"] == "follow_readings"
