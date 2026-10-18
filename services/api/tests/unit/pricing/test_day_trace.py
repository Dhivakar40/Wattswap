"""A full synthetic day: midnight, sunrise, noon, a sudden cloud event, sunset.

The same trace is printed in docs/pricing-model.md (`uv run python -m app.pricing.trace`) and a
test below fails if the document and the engine ever disagree.
"""

from itertools import pairwise

import pytest

from app.pricing.breaker import Mode
from app.pricing.engine import Tick
from app.pricing.params import PricingParams
from app.pricing.trace import (
    CHECKPOINTS,
    CLOUD_END,
    CLOUD_START,
    DAY_START,
    run_day,
)

P = PricingParams()
STEP = P.tick_seconds


@pytest.fixture(scope="module")
def day() -> list[Tick]:
    return run_day(P)


def at(day: list[Tick], sec: int) -> Tick:
    return day[sec // STEP]


def test_the_day_has_one_tick_per_15_seconds(day: list[Tick]) -> None:
    assert len(day) == 5760 and day[0].ts == DAY_START and day[-1].ts == DAY_START + 86_400 - STEP


def test_midnight_price_is_inventory_driven_and_matches_the_hand_calculation(
    day: list[Tick],
) -> None:
    t = at(day, 0)
    # S_eff = 0.5 * (24 kWh / 12 h) = 1.0 kW; D = 4.5 * 0.55 = 2.475 kW; 150000 * 2.475^0.6
    assert t.drivers["supply"]["generation_kw"] == "0.000000"
    assert t.drivers["supply"]["effective_kw"] == "1.000000"
    assert t.drivers["demand"]["demand_kw"] == "2.475000"
    assert t.raw_price == t.price == 258_367
    assert t.breaker.mode == Mode.NORMAL


def test_sunrise_lowers_the_raw_price_as_generation_arrives(day: list[Tick]) -> None:
    t = at(day, 6 * 3600 + 1800)
    assert t.drivers["weather"][0]["ghi_wm2"] == "165.000"
    assert t.raw_price < at(day, 0).raw_price
    assert float(t.drivers["supply"]["generation_kw"]) > 0


def test_noon_matches_the_hand_calculation(day: list[Tick]) -> None:
    t = at(day, 12 * 3600)
    # GHI 900, 30 C -> T_cell 57, derate 0.872, gen 3.1392 kW; S_eff = 4.1392; D = 4.5 * 0.70 = 3.15; ratio 0.761
    assert t.drivers["supply"]["generation_kw"] == "3.139200"
    assert t.drivers["supply"]["effective_kw"] == "4.139200"
    assert t.drivers["demand"]["demand_kw"] == "3.150000"
    assert t.raw_price == 127_329  # float cross-check: 127329.12
    assert abs(t.price - t.raw_price) < 50  # smoothed price has converged on the raw price


def test_a_sudden_cloud_event_spikes_raw_flags_an_anomaly_and_is_rate_limited(
    day: list[Tick],
) -> None:
    before = at(day, CLOUD_START - STEP)
    onset = at(day, CLOUD_START)
    assert before.breaker.mode == Mode.NORMAL
    # raw jumps by more than 60 % in a single tick...
    assert onset.raw_price > before.raw_price * 1.5
    assert onset.drivers["breaker"]["raw_move_bps"] > P.move_bps
    # ...the breaker raises an ANOMALY (it does NOT freeze), widens the hint, and logs the event
    assert onset.breaker.mode == Mode.ANOMALY and not onset.breaker.holds_price
    assert onset.band_bps == 450 and onset.drivers["band"]["widened"]
    assert [e["kind"] for e in onset.events] == ["anomaly_start"]
    # ...but the APPLIED price only creeps: at most 0.25 % per tick
    assert 0 < onset.price - before.price <= before.price * P.max_step_bps // 10_000
    # during the cloud the applied price climbs at the maximum allowed rate
    mid = at(day, CLOUD_START + 600)
    assert mid.drivers["smoothing"]["limiter"]["direction"] == "up"
    assert mid.price > onset.price and mid.price < mid.raw_price


def test_the_price_recovers_after_the_cloud_and_the_anomaly_clears(day: list[Tick]) -> None:
    after = at(day, CLOUD_END)
    assert (
        after.raw_price < 130_000 < after.price
    )  # raw already back down, applied still catching up
    assert after.drivers["smoothing"]["limiter"]["direction"] == "down"
    cleared = at(day, 14 * 3600)
    assert cleared.breaker.mode == Mode.NORMAL and cleared.band_bps == 150
    assert abs(cleared.price - cleared.raw_price) < 150


def test_sunset_price_rises_toward_the_evening_peak_but_is_nowhere_near_the_ceiling(
    day: list[Tick],
) -> None:
    t = at(day, 18 * 3600 + 1800)
    assert t.raw_price == 363_108 and 350_000 < t.price < 370_000  # evening peak, thin supply
    assert t.price < P.p_ceiling
    assert t.drivers["supply"]["effective_kw"] == "1.160000"  # ~0 generation + 1.0 inventory + ...


def test_the_whole_day_obeys_the_hard_invariants(day: list[Tick]) -> None:
    prices = [t.price for t in day]
    assert all(P.p_floor <= p <= P.p_ceiling for p in prices)
    assert max(prices) == 401_556 and min(prices) == 126_798  # never pinned to a bound
    assert max(prices) < P.p_ceiling * 7 // 10 and min(prices) > P.p_floor
    # per-tick limit, exactly
    for a, b in pairwise(prices):
        assert abs(b - a) * 10_000 <= a * P.max_step_bps
    # no 10-minute window of the APPLIED price ever reaches the breaker threshold
    ticks = P.move_window_s // STEP
    worst = max(
        abs(prices[i + ticks] - prices[i]) * 10_000 // prices[i] for i in range(len(prices) - ticks)
    )
    assert worst < P.move_bps
    # the day contains exactly one anomaly episode and never a freeze
    kinds = [e["kind"] for t in day for e in t.events]
    # two episodes: the cloud arriving (12:30) and the cloud clearing (13:15)
    assert kinds.count("anomaly_start") == 2 and kinds.count("anomaly_end") == 2
    assert "breaker_frozen" not in kinds
    assert all(t.breaker.mode != Mode.FROZEN for t in day)


def test_every_checkpoint_has_a_row() -> None:
    assert [c.label for c in CHECKPOINTS][:3] == ["midnight", "sunrise", "noon"]


# ---------------------------------------------------------------------- weather outage
def test_a_weather_outage_freezes_holds_and_then_recovers_with_hysteresis() -> None:
    day = run_day(P, stale_from=10 * 3600, stale_until=12 * 3600)
    modes = [t.breaker.mode for t in day]
    first_frozen = modes.index(Mode.FROZEN)
    # weather ages 15 s per tick from 10:00; stale once older than 1800 s -> frozen at 10:30:15
    assert first_frozen * STEP == 10 * 3600 + 1800 + STEP
    assert "weather stale" in str(day[first_frozen].breaker.reason)

    frozen_ticks = [t for t in day if t.breaker.mode == Mode.FROZEN]
    assert len({t.price for t in frozen_ticks}) == 1  # price held for the whole outage
    assert all(t.band_bps == 450 for t in frozen_ticks)

    # weather fresh again at 12:00: RECOVERING (still held) for 8 ticks, then NORMAL
    rec = [t for t in day if t.breaker.mode == Mode.RECOVERING]
    assert len(rec) == P.recovery_ticks - 1
    assert {t.price for t in rec} == {frozen_ticks[0].price}
    back = next(t for t in day if t.ts > frozen_ticks[-1].ts and t.breaker.mode == Mode.NORMAL)
    assert back.ts == frozen_ticks[-1].ts + P.recovery_ticks * STEP
    kinds = [e["kind"] for t in day for e in t.events]
    assert kinds.count("breaker_frozen") == 1 and kinds.count("breaker_recovered") == 1
