"""PV generation model: irradiance + temperature -> energy for one interval. SIMULATED."""

import random
from dataclasses import dataclass
from datetime import UTC, datetime

from meter_sim.solar import clear_sky_ghi
from meter_sim.weather import WeatherPoint, WeatherProvider, interpolate

PERFORMANCE_RATIO = 0.80  # inverter, wiring, soiling, mismatch
CELL_TEMP_RISE_PER_WM2 = 0.03  # cell temp ~= air temp + 0.03 x irradiance
TEMP_COEFF_PER_C = 0.004  # -0.4 %/degC above 25 degC
DEFAULT_SAFETY_BPS = 12_000  # MeterRegistry default (120 %)


def temp_derate(air_temp_c: float, ghi_wm2: float) -> float:
    cell = air_temp_c + CELL_TEMP_RISE_PER_WM2 * ghi_wm2
    return 1.0 - TEMP_COEFF_PER_C * max(0.0, cell - 25.0)


def pv_power_w(capacity_w: int, ghi_wm2: float, air_temp_c: float, pr: float) -> float:
    """AC power: capacity x (GHI / 1000) x performance ratio x temperature derate."""
    return max(0.0, capacity_w * (ghi_wm2 / 1000.0) * pr * temp_derate(air_temp_c, ghi_wm2))


def max_energy_wh(capacity_w: int, interval_seconds: int, safety_bps: int) -> int:
    """Mirror of MeterRegistry.maxEnergyWh."""
    return capacity_w * interval_seconds * safety_bps // (3600 * 10_000)


@dataclass(frozen=True)
class GenerationParams:
    performance_ratio: float = PERFORMANCE_RATIO
    noise_sigma: float = 0.03  # relative std-dev of multiplicative noise
    fallback_cloud_factor: float = 0.75  # clear-sky scale when no weather data is available
    fallback_temp_c: float = 25.0
    safety_bps: int = DEFAULT_SAFETY_BPS


@dataclass(frozen=True)
class Generation:
    wh: int
    source: str  # "open-meteo" | "clear-sky-fallback" | "mixed"
    ceiling_wh: int


class GenerationModel:
    def __init__(
        self, weather: WeatherProvider, params: GenerationParams, rng: random.Random
    ) -> None:
        self._weather = weather
        self._params = params
        self._rng = rng

    async def _sample(self, lat: float, lon: float, t: datetime) -> tuple[WeatherPoint, bool]:
        points = await self._weather.day(lat, lon, t.astimezone(UTC).date())
        if points is not None:
            return interpolate(points, t), True
        ghi = clear_sky_ghi(lat, lon, t) * self._params.fallback_cloud_factor
        return WeatherPoint(ghi, self._params.fallback_temp_c), False

    async def energy_wh(
        self, lat: float, lon: float, capacity_w: int, start_ts: int, seconds: int
    ) -> Generation:
        """Energy (Wh) produced in [start, start + seconds). Midpoint-rule integration."""
        n = max(1, min(12, seconds // 300))
        step = seconds / n
        total_w = 0.0
        used_weather = 0
        for i in range(n):
            t = datetime.fromtimestamp(start_ts + (i + 0.5) * step, tz=UTC)
            point, from_weather = await self._sample(lat, lon, t)
            used_weather += int(from_weather)
            total_w += pv_power_w(
                capacity_w, point.ghi_wm2, point.temp_c, self._params.performance_ratio
            )
        energy = total_w / n * seconds / 3600.0
        if energy > 0 and self._params.noise_sigma > 0:
            energy *= max(0.0, self._rng.gauss(1.0, self._params.noise_sigma))
        ceiling = max_energy_wh(capacity_w, seconds, self._params.safety_bps)
        source = (
            "open-meteo"
            if used_weather == n
            else "clear-sky-fallback"
            if used_weather == 0
            else "mixed"
        )
        return Generation(wh=min(round(energy), ceiling), source=source, ceiling_wh=ceiling)
