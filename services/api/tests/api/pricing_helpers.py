"""Helpers for the pricing service tests: a controllable clock and a respx-mocked Open-Meteo."""

import random
import time
from datetime import UTC, datetime
from typing import Any

import httpx
import respx
from eth_account import Account

from app.chain.tx import TxSender
from app.pricing.service import PricingService
from app.pricing.weather import OpenMeteo, WeatherService
from tests.conftest import Harness
from tests.helpers import login, new_meter

FORECAST = "https://open-meteo.test/v1/forecast"
ARCHIVE = "https://archive.open-meteo.test/v1/archive"


class FakeClock:
    """Starts at the real time (so it lines up with the chain) and only moves when told to."""

    def __init__(self) -> None:
        self.t = float(int(time.time()))

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


def location(
    clock_t: float, ghi: float = 800, temp: float = 30, hours: int = 72, start: int | None = None
) -> dict[str, Any]:
    """Constant weather for `hours` hours starting yesterday (UTC): covers 'now' either side."""
    first = start if start is not None else int(clock_t) // 86_400 * 86_400 - 86_400
    return {
        "hourly": {
            "time": [first + 3600 * i for i in range(hours)],
            "shortwave_radiation": [ghi] * hours,
            "temperature_2m": [temp] * hours,
            "cloud_cover": [20] * hours,
            "direct_radiation": [ghi * 0.7] * hours,
        }
    }


def batch_response(clock_t: float, n: int, **kw: Any) -> httpx.Response:
    body: Any = [location(clock_t, **kw) for _ in range(n)]
    return httpx.Response(200, json=body if n > 1 else body[0])


def mock_weather(clock: FakeClock, **kw: Any) -> respx.Route:
    """Answers every forecast request with constant weather for as many cells as were asked for."""

    def answer(request: httpx.Request) -> httpx.Response:
        n = len(request.url.params["latitude"].split(","))
        if "start_date" in request.url.params:  # a single-day request (history / demo clock)
            day = datetime.fromisoformat(request.url.params["start_date"]).replace(tzinfo=UTC)
            return httpx.Response(
                200, json=location(clock.t, hours=24, start=int(day.timestamp()), **kw)
            )
        return batch_response(clock.t, n, **kw)

    return respx.get(FORECAST).mock(side_effect=answer)


def make_service(h: Harness, clock: FakeClock, *, with_pricer: bool = True) -> PricingService:
    """A pricing service on the harness's database/Redis/chain, with a mocked weather provider."""
    chain = h.app.state.chain
    provider = OpenMeteo(
        httpx.AsyncClient(),
        forecast_url=FORECAST,
        archive_url=ARCHIVE,
        retries=0,
        retry_delay_s=0,
    )
    weather = WeatherService(provider, clock=clock, rng=random.Random(1), jitter=0.0)
    key = h.settings.pricer_private_key
    pricer = (
        TxSender(
            chain.client, chain.client.account(key.get_secret_value()), h.app.state.redis, "pricer"
        )
        if with_pricer and key is not None
        else None
    )
    return PricingService(
        h.settings,
        h.app.state.sessionmaker,
        h.app.state.redis,
        weather,
        chain=chain.client,
        pricer=pricer,
        wall=clock,
    )


async def owner_with_meter(
    h: Harness, *, capacity_w: int = 5000, lat: float = 13.0827, lon: float = 80.2707
) -> tuple[Any, dict[str, Any], str, str]:
    """A brand-new owner wallet (so its on-chain WATT balance is exactly 0) with one meter."""
    owner_key = "0x" + Account.create().key.hex()
    session = await login(h, owner_key)
    meter, device_key = await new_meter(h, session, capacity_w=capacity_w, lat=lat, lon=lon)
    return session, meter, device_key, owner_key
