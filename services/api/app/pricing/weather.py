"""Weather for the pricing engine: Open-Meteo behind a provider interface.

One batched request covers every active grid cell, polled every few minutes, so the request
rate does not depend on the number of meters. The response is untrusted input: every field is
validated, and anything malformed is treated like a failed request. The last good data is
kept and reported with its real age, which is what drives the circuit breaker.
"""

import asyncio
import logging
import math
import random
from collections import OrderedDict
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Protocol

import httpx

from app.core.metrics import WEATHER_REQUESTS
from app.pricing.region import Cell

log = logging.getLogger("wattswap.pricing.weather")

HOURLY_VARS = "shortwave_radiation,direct_radiation,cloud_cover,temperature_2m"
MAX_GHI = Decimal(1500)  # W/m2: above the solar constant, so certainly wrong
HISTORY_CACHE_DAYS = 512
ARCHIVE_LAG_DAYS = 6  # the archive endpoint trails real time by a few days


class WeatherError(Exception):
    """The weather response was unusable (transport failure, bad status, or malformed body)."""

    def __init__(self, kind: str, message: str, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.kind = kind  # timeout | connection | http_429 | http_5xx | http_4xx | malformed
        self.retry_after = retry_after


@dataclass(frozen=True)
class WeatherPoint:
    ghi_wm2: Decimal
    temp_c: Decimal
    cloud_cover_pct: Decimal | None = None
    direct_wm2: Decimal | None = None


Series = dict[int, WeatherPoint]  # unix hour (UTC) -> point


class WeatherProvider(Protocol):
    """What the engine needs from a weather source. Open-Meteo is the only implementation."""

    async def fetch_forecast(self, cells: Sequence[Cell]) -> dict[str, Series]: ...

    async def fetch_day(self, cell: Cell, day: date) -> Series: ...


# ------------------------------------------------------------------------- validation
def _num(value: object, lo: Decimal, hi: Decimal) -> Decimal | None:
    """A finite number within [lo, hi] as a Decimal, else None. Floats become Decimals via str()."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    if isinstance(value, float) and not math.isfinite(value):
        return None
    d = Decimal(str(value))
    return d if lo <= d <= hi else None


def parse_location(obj: object) -> Series:
    """One location's `hourly` block -> Series. Raises WeatherError('malformed') if unusable."""
    if not isinstance(obj, dict) or not isinstance(obj.get("hourly"), dict):
        raise WeatherError("malformed", "missing 'hourly' block")
    hourly = obj["hourly"]
    times = hourly.get("time")
    ghi = hourly.get("shortwave_radiation")
    temp = hourly.get("temperature_2m")
    if not all(isinstance(x, list) for x in (times, ghi, temp)):
        raise WeatherError("malformed", "hourly arrays missing or not lists")
    if not (len(times) == len(ghi) == len(temp)) or not times:
        raise WeatherError("malformed", "hourly arrays are empty or have different lengths")
    cloud = hourly.get("cloud_cover")
    direct = hourly.get("direct_radiation")
    cloud = cloud if isinstance(cloud, list) and len(cloud) == len(times) else None
    direct = direct if isinstance(direct, list) and len(direct) == len(times) else None

    series: Series = {}
    for i, ts in enumerate(times):
        if isinstance(ts, bool) or not isinstance(ts, int) or ts % 3600 != 0 or ts <= 0:
            raise WeatherError("malformed", f"bad timestamp at index {i}: {ts!r}")
        g = _num(ghi[i], Decimal(0), MAX_GHI)
        t = _num(temp[i], Decimal(-90), Decimal(70))
        if g is None or t is None:
            continue  # null / absurd hour: skip it, keep the rest
        series[ts] = WeatherPoint(
            ghi_wm2=g,
            temp_c=t,
            cloud_cover_pct=_num(cloud[i], Decimal(0), Decimal(100)) if cloud else None,
            direct_wm2=_num(direct[i], Decimal(0), MAX_GHI) if direct else None,
        )
    if not series:
        raise WeatherError("malformed", "no valid hour in the response")
    return series


def parse_batch(payload: object, cells: Sequence[Cell]) -> tuple[dict[str, Series], dict[str, str]]:
    """Map a (possibly multi-location) response onto `cells`.

    Returns (valid series per cell id, error per cell id). Whole-response problems raise.
    """
    items = payload if isinstance(payload, list) else [payload]
    if len(items) != len(cells):
        raise WeatherError(
            "malformed", f"expected {len(cells)} location(s) in the response, got {len(items)}"
        )
    ok: dict[str, Series] = {}
    bad: dict[str, str] = {}
    for cell, item in zip(cells, items, strict=True):
        try:
            ok[cell.id] = parse_location(item)
        except WeatherError as exc:
            bad[cell.id] = str(exc)
    if not ok:
        raise WeatherError("malformed", "; ".join(bad.values()) or "empty response")
    return ok, bad


def parse_retry_after(value: str | None) -> float | None:
    if value is None:
        return None
    try:
        seconds = float(value)
    except ValueError:
        return None
    return seconds if math.isfinite(seconds) and 0 <= seconds <= 86_400 else None


def interpolate_series(series: Series, ts: int) -> WeatherPoint | None:
    """Linear interpolation between the hourly points around `ts`; None outside the data."""
    hour = ts - ts % 3600
    a = series.get(hour)
    if a is None:
        return None
    b = series.get(hour + 3600)
    if b is None:
        return a
    frac = Decimal(ts - hour) / Decimal(3600)

    def mix(x: Decimal, y: Decimal) -> Decimal:
        return x + (y - x) * frac

    cloud = (
        mix(a.cloud_cover_pct, b.cloud_cover_pct)
        if a.cloud_cover_pct is not None and b.cloud_cover_pct is not None
        else a.cloud_cover_pct
    )
    return WeatherPoint(mix(a.ghi_wm2, b.ghi_wm2), mix(a.temp_c, b.temp_c), cloud, a.direct_wm2)


# --------------------------------------------------------------------------- Open-Meteo
class OpenMeteo:
    def __init__(
        self,
        client: httpx.AsyncClient,
        *,
        forecast_url: str = "https://api.open-meteo.com/v1/forecast",
        archive_url: str = "https://archive-api.open-meteo.com/v1/archive",
        timeout_s: float = 10.0,
        retries: int = 2,
        retry_delay_s: float = 1.0,
        today: Callable[[], date] = lambda: datetime.now(UTC).date(),
    ) -> None:
        self._client = client
        self._forecast_url = forecast_url
        self._archive_url = archive_url
        self._timeout = timeout_s
        self._retries = retries
        self._retry_delay = retry_delay_s
        self._today = today

    async def _get(self, url: str, params: dict[str, str]) -> object:
        last: WeatherError | None = None
        for attempt in range(self._retries + 1):
            try:
                resp = await self._client.get(url, params=params, timeout=self._timeout)
            except httpx.TimeoutException as exc:
                last = WeatherError("timeout", f"timeout: {exc}")
            except httpx.TransportError as exc:
                last = WeatherError("connection", f"connection error: {exc}")
            else:
                if resp.status_code == 429:
                    # Never hammer a rate limiter: report it, the caller backs off.
                    raise WeatherError(
                        "http_429",
                        "rate limited by Open-Meteo",
                        parse_retry_after(resp.headers.get("retry-after")),
                    )
                if resp.status_code >= 500:
                    last = WeatherError("http_5xx", f"HTTP {resp.status_code}")
                elif resp.status_code >= 400:
                    raise WeatherError("http_4xx", f"HTTP {resp.status_code}: {resp.text[:200]}")
                else:
                    try:
                        return resp.json()
                    except ValueError as exc:
                        raise WeatherError("malformed", f"body is not JSON: {exc}") from exc
            if attempt < self._retries:
                await asyncio.sleep(self._retry_delay * 2**attempt)
        assert last is not None
        raise last

    @staticmethod
    def _coords(cells: Sequence[Cell]) -> dict[str, str]:
        return {
            "latitude": ",".join(f"{c.center_lat:.4f}" for c in cells),
            "longitude": ",".join(f"{c.center_lon:.4f}" for c in cells),
            "hourly": HOURLY_VARS,
            "timezone": "GMT",
            "timeformat": "unixtime",
        }

    async def fetch_forecast(self, cells: Sequence[Cell]) -> dict[str, Series]:
        """Yesterday through tomorrow (UTC) for every cell, in ONE request."""
        params = {**self._coords(cells), "past_days": "1", "forecast_days": "2"}
        payload = await self._get(self._forecast_url, params)
        ok, bad = parse_batch(payload, cells)
        for cell_id, why in bad.items():
            log.warning("open-meteo: unusable data for cell %s: %s", cell_id, why)
        return ok

    async def fetch_day(self, cell: Cell, day: date) -> Series:
        """One UTC day for one cell (used by the demo clock and the backtest)."""
        old = (self._today() - day) > timedelta(days=ARCHIVE_LAG_DAYS)
        params = {
            **self._coords([cell]),
            "start_date": day.isoformat(),
            "end_date": day.isoformat(),
        }
        payload = await self._get(self._archive_url if old else self._forecast_url, params)
        ok, _ = parse_batch(payload, [cell])
        return ok[cell.id]


# ------------------------------------------------------------------------------ store
@dataclass
class CellWeather:
    series: Series = field(default_factory=dict)
    fetched_at: float | None = None  # wall clock of the last successful fetch


@dataclass(frozen=True)
class Lookup:
    point: WeatherPoint | None
    age_s: int
    source: str  # live | last_good | archive | none


@dataclass
class RefreshResult:
    status: str  # ok | partial | failed | skipped
    cells_updated: int = 0
    error: str | None = None
    next_allowed_in_s: float = 0.0


class WeatherService:
    """Holds the latest weather per cell and decides when the next request may be made."""

    def __init__(
        self,
        provider: WeatherProvider,
        *,
        clock: Callable[[], float],
        rng: random.Random | None = None,
        backoff_base_s: float = 30.0,
        backoff_max_s: float = 900.0,
        jitter: float = 0.2,
    ) -> None:
        self._provider = provider
        self._clock = clock
        self._rng = rng or random.Random()  # noqa: S311 - request jitter, not security
        self._base = backoff_base_s
        self._max = backoff_max_s
        self._jitter = jitter
        self._cells: dict[str, CellWeather] = {}
        self._history: OrderedDict[tuple[str, date], Series] = OrderedDict()
        self._failures = 0
        self._next_allowed = 0.0
        self.last_error: str | None = None

    # -- refresh -----------------------------------------------------------------------
    def _backoff(self, retry_after: float | None) -> float:
        self._failures += 1
        delay = min(self._max, self._base * 2 ** (self._failures - 1))
        delay *= 1 + self._rng.uniform(-self._jitter, self._jitter)
        return float(max(delay, retry_after or 0.0))

    async def refresh(self, cells: Sequence[Cell]) -> RefreshResult:
        now = self._clock()
        if not cells:
            return RefreshResult("ok")
        if now < self._next_allowed:
            WEATHER_REQUESTS.labels(result="skipped").inc()
            return RefreshResult("skipped", next_allowed_in_s=self._next_allowed - now)
        try:
            fetched = await self._provider.fetch_forecast(cells)
        except WeatherError as exc:
            delay = self._backoff(exc.retry_after)
            self._next_allowed = now + delay
            self.last_error = f"{exc.kind}: {exc}"
            WEATHER_REQUESTS.labels(result=exc.kind).inc()
            log.warning(
                "weather refresh failed (%s); keeping last good data, retry in %.0fs", exc, delay
            )
            return RefreshResult("failed", error=self.last_error, next_allowed_in_s=delay)
        self._failures = 0
        self._next_allowed = 0.0
        self.last_error = None
        for cell_id, series in fetched.items():
            slot = self._cells.setdefault(cell_id, CellWeather())
            slot.series = {**slot.series, **series}
            slot.fetched_at = now
        WEATHER_REQUESTS.labels(result="ok").inc()
        partial = len(fetched) < len(cells)
        return RefreshResult("partial" if partial else "ok", cells_updated=len(fetched))

    # -- lookup ------------------------------------------------------------------------
    def lookup(self, cell_id: str, ts: int) -> Lookup:
        """Weather for `ts` from the live series, with its real age."""
        slot = self._cells.get(cell_id)
        if slot is None or slot.fetched_at is None:
            return Lookup(None, 0, "none")
        point = interpolate_series(slot.series, ts)
        age = int(self._clock() - slot.fetched_at)
        if point is None:
            return Lookup(None, age, "none")
        return Lookup(point, age, "live" if self._failures == 0 else "last_good")

    def age_s(self, cell_id: str) -> int | None:
        slot = self._cells.get(cell_id)
        return (
            None
            if slot is None or slot.fetched_at is None
            else int(self._clock() - slot.fetched_at)
        )

    async def lookup_history(self, cell: Cell, ts: int) -> Lookup:
        """Weather for a past market time (the demo clock). Fetched once per cell per day."""
        live = self.lookup(cell.id, ts)
        if live.point is not None:
            return live
        day = datetime.fromtimestamp(ts, UTC).date()
        key = (cell.id, day)
        series = self._history.get(key)
        if series is None:
            try:
                series = await self._provider.fetch_day(cell, day)
            except WeatherError as exc:
                WEATHER_REQUESTS.labels(result=exc.kind).inc()
                log.warning("history fetch failed for %s %s: %s", cell.id, day, exc)
                return Lookup(None, 0, "none")
            WEATHER_REQUESTS.labels(result="ok").inc()
            self._history[key] = series
            while len(self._history) > HISTORY_CACHE_DAYS:
                self._history.popitem(last=False)
        # a day's series has no data beyond midnight; the next day is its own fetch
        point = interpolate_series(series, ts)
        return Lookup(point, 0, "archive" if point is not None else "none")
