"""Irradiance/temperature sources for the simulator.

Open-Meteo is untrusted external data: it is validated, retried with backoff, and any failure
falls back to a clear-sky model (the fallback is reported to the caller, never hidden).
"""

import asyncio
import logging
import math
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Protocol

import httpx

log = logging.getLogger("meter_sim.weather")

FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
ARCHIVE_URL = "https://archive-api.open-meteo.com/v1/archive"
ARCHIVE_LAG_DAYS = 6  # the archive endpoint trails real time by a few days
MAX_GHI_WM2 = 1500.0  # reject physically absurd values from the wire


@dataclass(frozen=True)
class WeatherPoint:
    ghi_wm2: float
    temp_c: float


class WeatherProvider(Protocol):
    """24 hourly points (UTC) for one calendar day at a location, or None if unavailable."""

    async def day(self, lat: float, lon: float, day: date) -> list[WeatherPoint] | None: ...


class NoWeather:
    """Deterministic stand-in: always unavailable, so the simulator uses its clear-sky model."""

    async def day(self, lat: float, lon: float, day: date) -> list[WeatherPoint] | None:
        return None


def parse_open_meteo(payload: object) -> list[WeatherPoint] | None:
    """Validate an Open-Meteo hourly response for exactly one UTC day; None if malformed."""
    if not isinstance(payload, dict):
        return None
    hourly = payload.get("hourly")
    if not isinstance(hourly, dict):
        return None
    ghi = hourly.get("shortwave_radiation")
    temp = hourly.get("temperature_2m")
    if not isinstance(ghi, list) or not isinstance(temp, list) or len(ghi) != 24 or len(temp) != 24:
        return None
    points: list[WeatherPoint] = []
    for g, t in zip(ghi, temp, strict=True):
        if (
            not isinstance(g, int | float)
            or not isinstance(t, int | float)
            or isinstance(g, bool)
            or isinstance(t, bool)
            or not math.isfinite(g)
            or not math.isfinite(t)
            or not 0 <= g <= MAX_GHI_WM2
            or not -90 <= t <= 70
        ):
            return None
        points.append(WeatherPoint(float(g), float(t)))
    return points


class OpenMeteoProvider:
    def __init__(
        self,
        client: httpx.AsyncClient | None = None,
        *,
        retries: int = 3,
        backoff_s: float = 0.5,
        timeout_s: float = 10.0,
        today: date | None = None,
    ) -> None:
        self._client = client or httpx.AsyncClient(timeout=timeout_s)
        self._retries = retries
        self._backoff = backoff_s
        self._today = today
        self._cache: dict[tuple[float, float, date], list[WeatherPoint] | None] = {}

    async def day(self, lat: float, lon: float, day: date) -> list[WeatherPoint] | None:
        key = (round(lat, 2), round(lon, 2), day)
        if key in self._cache and self._cache[key] is not None:
            return self._cache[key]
        points = await self._fetch(lat, lon, day)
        self._cache[key] = points
        return points

    async def _fetch(self, lat: float, lon: float, day: date) -> list[WeatherPoint] | None:
        today = self._today or datetime.now(UTC).date()
        url = ARCHIVE_URL if (today - day) > timedelta(days=ARCHIVE_LAG_DAYS) else FORECAST_URL
        params = {
            "latitude": f"{lat:.4f}",
            "longitude": f"{lon:.4f}",
            "hourly": "shortwave_radiation,temperature_2m",
            "start_date": day.isoformat(),
            "end_date": day.isoformat(),
            "timezone": "GMT",
        }
        for attempt in range(self._retries + 1):
            try:
                resp = await self._client.get(url, params=params)
                if resp.status_code == 429 or resp.status_code >= 500:
                    raise httpx.HTTPStatusError("retryable", request=resp.request, response=resp)
                resp.raise_for_status()
                points = parse_open_meteo(resp.json())
                if points is None:
                    log.warning("open-meteo returned an unexpected payload; using fallback")
                return points
            except (httpx.HTTPError, ValueError) as exc:
                if attempt == self._retries:
                    log.warning("open-meteo unavailable (%s); using clear-sky fallback", exc)
                    return None
                await asyncio.sleep(self._backoff * 2**attempt)
        return None

    async def aclose(self) -> None:
        await self._client.aclose()


def interpolate(points: list[WeatherPoint], t: datetime) -> WeatherPoint:
    """Linear interpolation of the day's hourly points at time `t` (UTC, same day)."""
    t = t.astimezone(UTC)
    pos = t.hour + t.minute / 60 + t.second / 3600
    lo = min(int(pos), 23)
    hi = min(lo + 1, 23)
    frac = pos - lo
    a, b = points[lo], points[hi]
    return WeatherPoint(
        a.ghi_wm2 + (b.ghi_wm2 - a.ghi_wm2) * frac, a.temp_c + (b.temp_c - a.temp_c) * frac
    )
