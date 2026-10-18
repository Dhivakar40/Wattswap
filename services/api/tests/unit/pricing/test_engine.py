"""Engine behaviour: smoothing chain, freeze/hold, band hint, drivers, determinism, restart."""

import json
from decimal import Decimal
from typing import Any

import pytest

from app.pricing.breaker import Mode, reset
from app.pricing.engine import EngineState, Tick, reset_breaker, restore_state, step
from app.pricing.params import PricingParams
from tests.unit.pricing.helpers import MIDNIGHT, cell, inputs

D = Decimal
P = PricingParams()
NIGHT = {"inventory_wh": 24_000}  # raw 146 599 at local midnight (see test_model)


def run(
    steps: int,
    make_inputs: Any,
    params: PricingParams = P,
    state: EngineState | None = None,
    t0: int = MIDNIGHT,
) -> tuple[EngineState, list[Tick]]:
    state = state or EngineState()
    ticks: list[Tick] = []
    for n in range(steps):
        now = t0 + n * params.tick_seconds
        state, tick = step(state, make_inputs(n), params, now)
        ticks.append(tick)
    return state, ticks


# ---------------------------------------------------------------------- smoothing chain
def test_first_tick_has_no_history_so_price_equals_raw() -> None:
    _, [t] = run(1, lambda n: inputs(cell(ghi="0"), **NIGHT))
    assert t.raw_price == 146_599 and t.price == 146_599
    assert t.ema_price == 146_599 and t.drivers["smoothing"]["previous"] is None
    assert t.breaker.mode == Mode.NORMAL and t.band_bps == 150  # un-widened hint


def test_ema_then_limiter_hand_computed_sequence() -> None:
    # tick 0: night, raw 146 599 -> price 146 599.
    # tick 1: demand spikes (20 kWh of bids): raw = 150000 * ratio**0.6 with the ratio capped
    #         at 10 -> 597 161. EMA = 0.2 * 597161 + 0.8 * 146599 = 236 711.4 (target 236 711).
    #         The limiter allows 146599 * 25 // 10000 = 366 -> price 146 965, direction "up".
    def make(n: int) -> Any:
        return inputs(cell(ghi="0"), **NIGHT, bids_wh=0 if n == 0 else 100_000)

    _, [t0, t1] = run(2, make)
    assert (t0.price, t1.raw_price) == (146_599, 597_161)
    assert t1.drivers["smoothing"]["ema"] == "236711.400"
    assert t1.drivers["smoothing"]["target"] == 236_711
    assert t1.price == 146_599 + 366 == 146_965
    assert t1.drivers["smoothing"]["limiter"]["direction"] == "up"
    assert "limiter_up" in t1.drivers["clamps"] and "ratio_cap" in t1.drivers["clamps"]


def test_clamps_are_reported() -> None:
    _, [t] = run(1, lambda n: inputs(cell(ghi="0"), bids_wh=100_000))
    assert "ratio_cap" in t.drivers["clamps"]
    _, [t] = run(1, lambda n: inputs(cell(ghi="0")))
    assert t.drivers["clamps"] == ["s_min"]  # floor on supply applied, nothing else
    _, [t] = run(1, lambda n: inputs(cell(ghi="0"), inventory_wh=2_000_000))
    assert "raw_floor" in t.drivers["clamps"] and t.price == P.p_floor


def test_price_never_leaves_the_floor_ceiling_even_if_params_change_under_it() -> None:
    state, _ = run(3, lambda n: inputs(cell(ghi="0"), **NIGHT))
    tight = PricingParams(p_floor=140_000, p_base=145_000, p_ceiling=146_000)
    _, tick = step(state, inputs(cell(ghi="0"), **NIGHT), tight, MIDNIGHT + 100)
    assert tick.price == 146_000 and "final_ceiling" in tick.drivers["clamps"]


# ------------------------------------------------------------------- breaker in the engine
def test_stale_weather_freezes_the_price_and_widens_the_band() -> None:
    def make(n: int) -> Any:
        age = 0 if n < 4 else 1801 + n  # stale from tick 4
        return inputs(cell(ghi="0", age_s=age), **NIGHT, bids_wh=0 if n < 4 else 50_000)

    _, ticks = run(10, make)
    assert all(t.breaker.mode == Mode.NORMAL for t in ticks[:4])
    frozen = ticks[4:]
    assert all(t.breaker.mode == Mode.FROZEN and t.weather_stale for t in frozen)
    assert {t.price for t in frozen} == {ticks[3].price}  # HELD, even though raw soared
    assert frozen[0].raw_price > ticks[3].price * 2
    assert {t.ema_price for t in frozen} == {ticks[3].ema_price}  # EMA is not advanced either
    assert all(t.band_bps == 450 for t in frozen)  # 150 * 3
    assert "held" in frozen[0].drivers["clamps"]
    assert "weather stale" in frozen[0].drivers["breaker"]["stale_detail"]
    assert [e["kind"] for e in frozen[0].events] == ["breaker_frozen"]


def test_recovery_resumes_repricing_through_the_limiter() -> None:
    def make(n: int) -> Any:
        age = 5000 if 2 <= n < 6 else 0
        return inputs(cell(ghi="0", age_s=age), **NIGHT, bids_wh=100_000 if n >= 2 else 0)

    _, ticks = run(30, make)
    modes = [t.breaker.mode for t in ticks]
    assert modes[1] == Mode.NORMAL and modes[2] == Mode.FROZEN and modes[6] == Mode.RECOVERING
    assert modes[13] == Mode.NORMAL  # recovery_ticks = 8 healthy ticks (n = 6..13)
    held = ticks[1].price
    assert all(t.price == held for t in ticks[2:13])  # held through FROZEN and RECOVERING
    # then the price moves again, but only by the limiter's 0.25 % per tick
    assert ticks[14].price > held
    assert ticks[14].price - ticks[13].price <= ticks[13].price * 25 // 10_000


def test_halt_quotes_flag_is_only_set_when_configured_and_frozen() -> None:
    stale = lambda n: inputs(cell(ghi="0", age_s=9999), **NIGHT)  # noqa: E731
    _, [t] = run(1, stale)
    assert t.breaker.mode == Mode.FROZEN and not t.halted
    _, [t] = run(1, stale, PricingParams(halt_quotes_on_freeze=True))
    assert t.halted


def test_cold_start_while_stale_holds_the_neutral_price() -> None:
    _, [t] = run(1, lambda n: inputs(cell(ghi="0", source="none", age_s=0), **NIGHT))
    assert t.breaker.mode == Mode.FROZEN and t.price == P.p_base


def test_raw_anomaly_widens_the_band_but_keeps_repricing() -> None:
    def make(n: int) -> Any:
        return inputs(cell(ghi="0"), **NIGHT, bids_wh=0 if n < 3 else 100_000)

    _, ticks = run(6, make)
    spike = ticks[3]
    assert spike.breaker.mode == Mode.ANOMALY and spike.events[0]["kind"] == "anomaly_start"
    assert spike.drivers["breaker"]["raw_move_bps"] > 1500
    assert spike.band_bps == 450 and spike.drivers["band"]["widened"] is True
    assert spike.price > ticks[2].price  # still repricing (limited, but moving)
    assert spike.drivers["breaker"]["mode"] != "FROZEN"


def test_band_hint_is_never_wider_than_the_onchain_band() -> None:
    _, [t] = run(1, lambda n: inputs(cell(ghi="0", age_s=9999), **NIGHT, band=300))
    assert t.breaker.widens_band and t.band_bps == 300  # 450 capped at the on-chain 300
    _, [t] = run(1, lambda n: inputs(cell(ghi="0"), **NIGHT, band=100))
    assert t.band_bps == 100  # even the normal hint (150) is capped


def test_admin_reset_clears_a_freeze_and_repricing_resumes() -> None:
    state, ticks = run(3, lambda n: inputs(cell(ghi="0", age_s=9999), **NIGHT))
    assert ticks[-1].breaker.mode == Mode.FROZEN
    state = reset_breaker(state, MIDNIGHT + 60)
    assert state.breaker == reset(state.breaker, MIDNIGHT + 60)
    _, tick = step(state, inputs(cell(ghi="0"), **NIGHT), P, MIDNIGHT + 75)
    assert tick.breaker.mode == Mode.NORMAL and "held" not in tick.drivers["clamps"]


# -------------------------------------------------------------------------- drivers
def test_every_tick_carries_a_complete_price_drivers_breakdown() -> None:
    _, [t] = run(
        1, lambda n: inputs(cell(ghi="800", cloud="40"), **NIGHT, bids_wh=2000, fills_wh=1000)
    )
    d = t.drivers
    assert set(d) >= {
        "version", "market_time", "local_solar_hour", "profile_multiplier", "supply", "demand",
        "price", "smoothing", "clamps", "weather", "breaker", "band", "attribution", "terms",
    }  # fmt: skip
    assert d["supply"]["generation_kw"] == "2.828800"
    assert (
        d["supply"]["inventory_kw"] == "2.000000"
        and d["supply"]["inventory_source"] == "held_balances"
    )
    assert d["supply"]["effective_kw"] == "3.828800"
    assert d["demand"]["bids_kw"] == "2.000000" and d["demand"]["velocity_kw"] == "0.500000"
    w = d["weather"][0]
    assert (
        w["ghi_wm2"] == "800.000" and w["cloud_cover_pct"] == "40.0" and w["cell_temp_c"] == "54.00"
    )
    assert w["derate"] == "0.884000" and w["generation_kw"] == "2.828800" and w["source"] == "live"
    assert d["breaker"]["mode"] == "NORMAL" and d["breaker"]["move_limit_bps"] == 1500
    assert d["band"] == {
        "hint_bps": 150,
        "published_bps": 150,
        "onchain_band_bps": 500,
        "widened": False,
    }


def test_drivers_are_json_safe_and_contain_no_floats() -> None:
    _, ticks = run(3, lambda n: inputs(cell(ghi="500"), **NIGHT, bids_wh=1000 * n))

    def walk(x: object) -> None:
        assert not isinstance(x, float), x
        if isinstance(x, dict):
            for v in x.values():
                walk(v)
        elif isinstance(x, list | tuple):
            for v in x:
                walk(v)

    for t in ticks:
        json.dumps(t.drivers)  # must not raise
        walk(t.drivers)
        assert isinstance(t.price, int) and isinstance(t.raw_price, int)
        assert isinstance(t.supply_w, int) and isinstance(t.demand_w, int)
        assert not isinstance(t.price, bool)


def test_attribution_splits_a_demand_driven_move() -> None:
    # Only demand changes between the ticks: 0.9625 kW -> 0.9625 + 1 kW of bids.
    def make(n: int) -> Any:
        return inputs(cell(ghi="0"), **NIGHT, bids_wh=0 if n == 0 else 1000)

    _, [_, t1] = run(2, make)
    a = t1.drivers["attribution"]
    # eps * ln(1.9625 / 0.9625) * 10000 = 0.6 * 0.712440 * 10000 = 4274.6 bps; supply unchanged
    assert float(a["from_demand_bps"]) == pytest.approx(4274.6, abs=0.1)
    assert float(a["from_supply_bps"]) == 0 and float(a["of_which_generation_bps"]) == 0
    assert a["approximate"] is False


def test_attribution_splits_a_supply_move_between_generation_and_inventory() -> None:
    # Sunrise: generation appears (inventory unchanged) -> the whole supply effect is generation.
    def make(n: int) -> Any:
        return inputs(cell(ghi="0" if n == 0 else "400"), **NIGHT)

    _, [_, t1] = run(2, make)
    a = t1.drivers["attribution"]
    assert float(a["from_supply_bps"]) < 0  # more supply -> lower price
    assert a["of_which_generation_bps"] == a["from_supply_bps"]
    assert float(a["of_which_inventory_bps"]) == 0


def test_first_tick_has_no_attribution() -> None:
    _, [t] = run(1, lambda n: inputs(cell(ghi="0"), **NIGHT))
    assert t.drivers["attribution"] is None


# ------------------------------------------------------------ determinism and restart
def test_the_engine_is_deterministic() -> None:
    def make(n: int) -> Any:
        return inputs(cell(ghi=str(n % 900), temp="25"), **NIGHT, bids_wh=(n * 37) % 5000)

    _, a = run(200, make)
    _, b = run(200, make)
    assert [json.dumps(t.drivers, sort_keys=True) for t in a] == [
        json.dumps(t.drivers, sort_keys=True) for t in b
    ]
    assert [(t.price, t.raw_price, t.ema_price) for t in a] == [
        (t.price, t.raw_price, t.ema_price) for t in b
    ]


def test_restart_from_persisted_ticks_continues_identically() -> None:
    def make(n: int) -> Any:
        return inputs(cell(ghi=str((n * 13) % 900), temp="25"), **NIGHT, bids_wh=(n * 91) % 7000)

    _, full = run(120, make)

    _, first = run(60, make)
    rows = [
        {
            "ts": t.ts,
            "price": t.price,
            "raw_price": t.raw_price,
            "drivers": json.loads(json.dumps(t.drivers)),
        }
        for t in first
    ]
    restored = restore_state(rows, P)  # a fresh process reading the database
    _, second = run(60, lambda n: make(n + 60), state=restored, t0=MIDNIGHT + 60 * P.tick_seconds)
    assert [(t.price, t.raw_price, t.band_bps, t.breaker.mode) for t in full[60:]] == [
        (t.price, t.raw_price, t.band_bps, t.breaker.mode) for t in second
    ]


def test_restore_with_no_rows_is_a_cold_start() -> None:
    assert restore_state([], P) == EngineState()


# ----------------------------------------------------------------- volatility, units
def test_volatility_is_published_once_there_is_history() -> None:
    _, ticks = run(5, lambda n: inputs(cell(ghi="0"), **NIGHT, bids_wh=500 * n))
    assert ticks[0].vol_bps is None and ticks[1].vol_bps is None
    assert ticks[4].vol_bps is not None and ticks[4].vol_bps > 0


def test_supply_and_demand_are_stored_as_integer_watts() -> None:
    _, [t] = run(1, lambda n: inputs(cell(ghi="800"), **NIGHT))
    # 3.8288 kW -> 3829 W; 0.9625 kW = 962.5 W -> 962 (round half to even, deterministic)
    assert t.supply_w == 3829 and t.demand_w == 962
