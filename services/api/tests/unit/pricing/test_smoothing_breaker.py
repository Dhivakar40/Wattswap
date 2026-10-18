"""EMA, rate limiter, volatility and the circuit-breaker state machine."""

import math
from decimal import Decimal

from hypothesis import given, settings
from hypothesis import strategies as st

from app.pricing.breaker import BreakerState, Mode, evaluate, raw_move_bps, reset
from app.pricing.params import PricingParams, max_applied_move_bps
from app.pricing.smoothing import clamp, ema, limit_step, realized_vol_bps

D = Decimal
P = PricingParams()


# ------------------------------------------------------------------------------- EMA
def test_ema_seeds_with_the_first_raw_price_then_blends() -> None:
    assert ema(None, 200_000, D("0.2")) == 200_000
    # 0.2 * 200000 + 0.8 * 100000 = 120000, then 0.2 * 200000 + 0.8 * 120000 = 136000
    first = ema(D(100_000), 200_000, D("0.2"))
    assert first == 120_000
    assert ema(first, 200_000, D("0.2")) == 136_000


def test_ema_alpha_one_is_no_smoothing_and_converges() -> None:
    assert ema(D(100_000), 150_000, D(1)) == 150_000
    value = D(100_000)
    for _ in range(60):
        value = ema(value, 150_000, D("0.2"))
    assert abs(value - 150_000) < 1  # 0.8 ** 60 * 50000 ~ 0.08


def test_clamp() -> None:
    assert clamp(5, 10, 20) == 10 and clamp(25, 10, 20) == 20 and clamp(15, 10, 20) == 15


# --------------------------------------------------------------------------- limiter
def test_limiter_worked_example() -> None:
    # 150000 * 25 / 10000 = 375
    up = limit_step(150_000, 200_000, 25)
    assert (up.price, up.direction, up.max_up, up.max_down) == (150_375, "up", 150_375, 149_625)
    assert limit_step(150_000, 100_000, 25).price == 149_625
    assert limit_step(150_000, 100_000, 25).direction == "down"
    inside = limit_step(150_000, 150_100, 25)
    assert inside.price == 150_100 and inside.direction is None


def test_limiter_rounds_toward_the_previous_price() -> None:
    # 20001 * 25 / 10000 = 50.0025 -> the allowed delta is 50, never 51
    assert limit_step(20_001, 600_000, 25).price == 20_051
    assert limit_step(20_001, 1, 25).price == 19_951


def test_limiter_exact_bound_is_never_exceeded() -> None:
    for prev in (20_000, 20_001, 33_333, 150_000, 599_999, 600_000):
        up = limit_step(prev, 10**9, 25).price
        dn = limit_step(prev, 1, 25).price
        assert (up - prev) * 10_000 <= prev * 25
        assert (prev - dn) * 10_000 <= prev * 25


def test_limiter_alone_can_never_reach_the_breaker_threshold_within_its_window() -> None:
    """The consistency requirement between limiter and breaker, shown three ways."""
    ticks = P.move_window_s // P.tick_seconds  # 600 / 15 = 40
    assert ticks == 40

    # 1. Closed form: (1.0025)^40 - 1 = 10.50 % up, 1 - 0.9975^40 = 9.53 % down, both < 15 %.
    up_bps, down_bps = max_applied_move_bps(P.max_step_bps, ticks)
    assert up_bps == 1051 and down_bps == 953
    assert max(up_bps, down_bps) < P.move_bps == 1500

    # 2. Worst case, executed: a raw price pinned at the opposite extreme for the whole window.
    price = P.p_base
    path = [price]
    for _ in range(2000):
        price = limit_step(price, P.p_ceiling, P.max_step_bps).price
        path.append(price)
    worst_up = max(
        (path[i + ticks] - path[i]) * 10_000 // path[i] for i in range(len(path) - ticks)
    )
    assert worst_up <= up_bps and worst_up < P.move_bps
    for _ in range(2000):
        price = limit_step(price, P.p_floor, P.max_step_bps).price
        path.append(price)
    worst_move = max(
        abs(path[i + ticks] - path[i]) * 10_000 // path[i] for i in range(len(path) - ticks)
    )
    assert worst_move < P.move_bps

    # 3. The breaker, fed that worst-case APPLIED path, never reports an anomaly.
    for i in range(ticks, len(path)):
        window = [(j * P.tick_seconds, path[j]) for j in range(i - ticks, i)]
        assert raw_move_bps(window, path[i], i * P.tick_seconds, P.move_window_s) < P.move_bps


@settings(max_examples=200, deadline=None)
@given(
    start=st.integers(P.p_floor, P.p_ceiling),
    targets=st.lists(st.integers(1, 10**7), min_size=45, max_size=150),
)
def test_limiter_property_any_targets_stay_below_the_breaker_threshold(
    start: int, targets: list[int]
) -> None:
    ticks = P.move_window_s // P.tick_seconds
    path = [start]
    for t in targets:
        path.append(clamp(limit_step(path[-1], t, P.max_step_bps).price, P.p_floor, P.p_ceiling))
    for i in range(len(path) - ticks):
        for k in range(1, ticks + 1):
            assert abs(path[i + k] - path[i]) * 10_000 < P.move_bps * path[i]


# ----------------------------------------------------------------------- volatility
def test_realized_volatility_worked_example() -> None:
    # prices 100, 101, 100: |r| = ln(1.01) = 0.00995033 for both returns,
    # sigma_tick = 0.00995033, * sqrt(240 ticks/hour) * 10000 = 1541.50 bps per sqrt(hour)
    vol = realized_vol_bps([100, 101, 100], D(240))
    assert vol is not None
    assert math.isclose(float(vol), 1541.4986, rel_tol=1e-6)


def test_realized_volatility_needs_two_returns_and_is_zero_for_flat_prices() -> None:
    assert realized_vol_bps([100], D(240)) is None
    assert realized_vol_bps([100, 101], D(240)) is None
    assert realized_vol_bps([100, 100, 100, 100], D(240)) == 0


# ------------------------------------------------------------------------ breaker
def tick_eval(
    state: BreakerState,
    now: int,
    *,
    stale: bool = False,
    history: list[tuple[int, int]] | None = None,
    raw: int = 100_000,
) -> tuple[BreakerState, list[dict[str, object]], int]:
    return evaluate(
        state,
        now=now,
        weather_stale=stale,
        stale_detail="weather stale (> 1800s) for cell(s): 26:0 (2000s old)",
        raw_history=history or [],
        raw_now=raw,
        params=P,
    )


def test_move_measure_is_relative_strict_and_windowed() -> None:
    hist = [(0, 100_000)]
    assert raw_move_bps(hist, 115_000, 300, 600) == 1500  # exactly 15 %
    assert raw_move_bps(hist, 116_000, 300, 600) == 1600
    assert raw_move_bps(hist, 85_000, 300, 600) == 1500  # downward, relative to the OLD value
    assert raw_move_bps(hist, 84_900, 300, 600) == 1510
    assert raw_move_bps(hist, 116_000, 601, 600) == 0  # sample is outside the window
    assert raw_move_bps([(0, 100_000), (200, 140_000)], 150_000, 300, 600) == 5000  # largest wins


def test_normal_to_anomaly_only_above_the_threshold_and_never_freezes() -> None:
    s = BreakerState()
    s, ev, move = tick_eval(s, 300, history=[(0, 100_000)], raw=115_000)  # exactly 15 %: no
    assert s.mode == Mode.NORMAL and not ev and move == 1500

    s, ev, move = tick_eval(s, 315, history=[(0, 100_000)], raw=116_000)
    assert s.mode == Mode.ANOMALY and s.since == 315
    assert [e["kind"] for e in ev] == ["anomaly_start"]
    assert not s.holds_price and s.widens_band  # reprices normally, wider band hint


def test_anomaly_clears_after_the_hold_period_and_retrigger_resets_it() -> None:
    s = BreakerState(Mode.ANOMALY, "x", 0, 0, 0)
    for n in range(P.anomaly_hold_ticks - 1):
        s, ev, _ = tick_eval(s, 1000 + n)
        assert s.mode == Mode.ANOMALY and not ev
    # a new violation restarts the countdown
    s, _, _ = tick_eval(s, 2000, history=[(1990, 100_000)], raw=130_000)
    assert s.mode == Mode.ANOMALY and s.quiet_ticks == 0
    kinds: list[object] = []
    for n in range(P.anomaly_hold_ticks):
        s, ev, _ = tick_eval(s, 3000 + n)
        kinds += [e["kind"] for e in ev]
    assert s.mode == Mode.NORMAL and kinds == ["anomaly_end"]


def test_stale_weather_freezes_from_any_state_and_holds_the_price() -> None:
    for start in (Mode.NORMAL, Mode.ANOMALY, Mode.RECOVERING):
        s, ev, _ = tick_eval(BreakerState(start, None, 0, 3, 3), 5000, stale=True)
        assert s.mode == Mode.FROZEN and s.holds_price and s.widens_band
        assert ev[0]["kind"] == "breaker_frozen" and "weather stale" in str(s.reason)
    # staying frozen emits no new event
    s, ev, _ = tick_eval(s, 5015, stale=True)
    assert s.mode == Mode.FROZEN and not ev


def test_recovery_needs_consecutive_healthy_ticks_and_restarts_if_stale_again() -> None:
    s = BreakerState(Mode.FROZEN, "stale", 0)
    s, ev, _ = tick_eval(s, 100)  # fresh weather again
    assert s.mode == Mode.RECOVERING and s.healthy_ticks == 1 and s.holds_price
    assert ev[0]["kind"] == "breaker_recovering"
    for n in range(2, P.recovery_ticks):
        s, ev, _ = tick_eval(s, 100 + n)
        assert s.mode == Mode.RECOVERING and s.healthy_ticks == n and not ev
    s_relapse, _, _ = tick_eval(s, 500, stale=True)
    assert s_relapse.mode == Mode.FROZEN  # one bad tick resets the hysteresis

    s, ev, _ = tick_eval(s, 600)  # the recovery_ticks-th healthy tick
    assert s.mode == Mode.NORMAL and ev[0]["kind"] == "breaker_recovered"
    assert not s.holds_price and not s.widens_band


def test_admin_reset_forces_normal() -> None:
    for start in (Mode.ANOMALY, Mode.FROZEN, Mode.RECOVERING):
        assert reset(BreakerState(start, "r", 1, 2, 3), 99) == BreakerState(Mode.NORMAL, None, 99)


def test_breaker_state_json_roundtrip() -> None:
    s = BreakerState(Mode.RECOVERING, "weather fresh again", 123, 4, 0)
    assert BreakerState.from_json(s.to_json()) == s
