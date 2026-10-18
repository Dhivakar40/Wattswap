import random
from datetime import UTC, date, datetime

import pytest

from meter_sim.model import (
    GenerationModel,
    GenerationParams,
    max_energy_wh,
    pv_power_w,
    temp_derate,
)
from meter_sim.solar import clear_sky_ghi, solar_elevation_deg
from meter_sim.weather import NoWeather, WeatherPoint

CHENNAI = (13.0827, 80.2707)


def utc(y: int, mo: int, d: int, h: int, mi: int = 0) -> datetime:
    return datetime(y, mo, d, h, mi, tzinfo=UTC)


class ConstantWeather:
    def __init__(self, ghi: float, temp: float) -> None:
        self.points = [WeatherPoint(ghi, temp)] * 24

    async def day(self, lat: float, lon: float, day: date) -> list[WeatherPoint] | None:
        return self.points


def test_sun_is_down_at_night_and_up_at_local_noon() -> None:
    lat, lon = CHENNAI
    assert clear_sky_ghi(lat, lon, utc(2026, 3, 20, 18)) == 0.0  # 23:30 local
    noon = clear_sky_ghi(lat, lon, utc(2026, 3, 20, 6, 40))  # ~ solar noon at 80.27E
    assert 900 < noon < 1100
    assert solar_elevation_deg(lat, lon, utc(2026, 3, 20, 6, 40)) > 75


def test_equinox_elevation_matches_textbook() -> None:
    # At the equinox, solar-noon elevation is ~ 90 - |latitude|.
    elev = solar_elevation_deg(40.0, 0.0, utc(2026, 3, 20, 12))
    assert elev == pytest.approx(50.0, abs=1.5)


def test_summer_beats_winter_in_the_north_and_flips_in_the_south() -> None:
    assert clear_sky_ghi(51.0, 0.0, utc(2026, 6, 21, 12)) > clear_sky_ghi(
        51.0, 0.0, utc(2026, 12, 21, 12)
    )
    assert clear_sky_ghi(-33.0, 151.0, utc(2026, 12, 21, 2)) > clear_sky_ghi(
        -33.0, 151.0, utc(2026, 6, 21, 2)
    )


def test_temp_derate_worked_example() -> None:
    # air 30C, GHI 800: cell = 30 + 0.03*800 = 54C -> 1 - 0.004*29 = 0.884
    assert temp_derate(30.0, 800.0) == pytest.approx(0.884)
    assert temp_derate(10.0, 0.0) == 1.0  # cold cells are not boosted above nameplate


def test_power_worked_example() -> None:
    # 5 kW x 0.8 (ghi/1000) x 0.8 (PR) x 0.884 = 2828.8 W
    assert pv_power_w(5000, 800.0, 30.0, 0.8) == pytest.approx(2828.8)
    assert pv_power_w(5000, 0.0, 30.0, 0.8) == 0.0


def test_max_energy_matches_registry_formula() -> None:
    assert max_energy_wh(5000, 900, 12_000) == 1500  # same number as the Solidity unit test
    assert max_energy_wh(5000, 60, 12_000) == 100
    assert max_energy_wh(5000, 3600, 12_000) == 6000


async def test_energy_uses_the_interval_length() -> None:
    model = GenerationModel(
        ConstantWeather(800.0, 30.0), GenerationParams(noise_sigma=0), random.Random(1)
    )
    wh_15 = await model.energy_wh(*CHENNAI, 5000, 1_800_000_000, 900)
    wh_60 = await model.energy_wh(*CHENNAI, 5000, 1_800_000_000, 3600)
    assert wh_15.wh == 707  # 2828.8 W x 0.25 h = 707.2 Wh
    assert wh_60.wh == 2829  # 2828.8 W x 1 h
    assert wh_15.source == "open-meteo"


async def test_noise_is_reproducible_and_bounded_by_ceiling() -> None:
    params = GenerationParams(noise_sigma=0.05)
    a = GenerationModel(ConstantWeather(800.0, 25.0), params, random.Random(42))
    b = GenerationModel(ConstantWeather(800.0, 25.0), params, random.Random(42))
    ra = [(await a.energy_wh(*CHENNAI, 5000, 1_800_000_000 + i * 900, 900)).wh for i in range(5)]
    rb = [(await b.energy_wh(*CHENNAI, 5000, 1_800_000_000 + i * 900, 900)).wh for i in range(5)]
    assert ra == rb
    assert len(set(ra)) > 1  # the noise actually varies
    # An absurd performance ratio is clamped to the registry ceiling, never above it.
    hot = GenerationModel(
        ConstantWeather(1400.0, 25.0),
        GenerationParams(performance_ratio=5.0, noise_sigma=0),
        random.Random(1),
    )
    g = await hot.energy_wh(*CHENNAI, 5000, 1_800_000_000, 900)
    assert g.wh == g.ceiling_wh == 1500


async def test_fallback_is_clear_sky_scaled_and_labeled() -> None:
    model = GenerationModel(NoWeather(), GenerationParams(noise_sigma=0), random.Random(1))
    noon = int(utc(2026, 3, 20, 6, 30).timestamp())
    night = int(utc(2026, 3, 20, 18, 0).timestamp())
    day_gen = await model.energy_wh(*CHENNAI, 5000, noon, 900)
    night_gen = await model.energy_wh(*CHENNAI, 5000, night, 900)
    assert day_gen.source == "clear-sky-fallback"
    assert 500 < day_gen.wh <= day_gen.ceiling_wh
    assert night_gen.wh == 0
