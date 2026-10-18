"""Every model formula against a hand-computed value.

The derivations are in the comments so a reviewer can redo them with a calculator.
"""

from decimal import Decimal

import pytest

from app.pricing.model import (
    cell_temperature,
    demand_terms,
    generation_kw,
    load_multiplier,
    local_solar_hour,
    raw_price,
    supply_terms,
    temp_derate,
)
from app.pricing.params import DEFAULT_LOAD_PROFILE, PricingParams
from tests.unit.pricing.helpers import MIDNIGHT, cell, inputs

D = Decimal
P = PricingParams()


# ------------------------------------------------------------------------ physical pieces
def test_cell_temperature() -> None:
    # 30 + 0.03 * 800 = 54
    assert cell_temperature(D(30), D(800), D("0.03")) == D(54)


def test_temp_derate() -> None:
    # 1 - 0.004 * (54 - 25) = 1 - 0.116 = 0.884
    assert temp_derate(D(54), D("0.004")) == D("0.884")
    # cold cells are not boosted above nameplate: max(0, 10 - 25) = 0 -> 1
    assert temp_derate(D(10), D("0.004")) == D(1)
    # exactly 25 C is neutral
    assert temp_derate(D(25), D("0.004")) == D(1)


def test_generation_worked_example() -> None:
    # 5 kW * (800/1000) * 0.80 * 0.884 = 5 * 0.8 * 0.8 * 0.884 = 3.2 * 0.884 = 2.8288 kW
    assert generation_kw(5000, D(800), D(30), P) == D("2.8288")


def test_generation_is_zero_without_sun_and_never_negative() -> None:
    assert generation_kw(5000, D(0), D(30), P) == 0
    # absurd heat would drive the derate negative; generation is clamped at 0
    assert generation_kw(5000, D(1000), D(900), P) == 0


# ---------------------------------------------------------------------------- local hour
def test_local_solar_hour() -> None:
    assert local_solar_hour(MIDNIGHT, 0) == 0
    assert local_solar_hour(MIDNIGHT + 12 * 3600, 0) == 12
    # 90 degrees east is +6 hours
    assert local_solar_hour(MIDNIGHT + 12 * 3600, 90_000_000) == 18
    # 90 degrees west wraps below 0 -> +24: 00:00 UTC is 18:00 local
    assert local_solar_hour(MIDNIGHT, -90_000_000) == 18
    # 80.2707 E is +5.35138 h: 06:30 UTC -> 11:51:05 local
    h = local_solar_hour(MIDNIGHT + 6 * 3600 + 1800, 80_270_700)
    assert h == D("6.5") + D("80.2707") / 15


def test_load_multiplier_interpolates_and_wraps() -> None:
    # hour 19 = 1.15, hour 20 = 1.05: halfway is 1.10
    assert load_multiplier(DEFAULT_LOAD_PROFILE, D("19.5")) == D("1.10")
    assert load_multiplier(DEFAULT_LOAD_PROFILE, D(19)) == D("1.15")
    # between 23 (0.62) and 0 (0.55): at 23.5 -> 0.585
    assert load_multiplier(DEFAULT_LOAD_PROFILE, D("23.5")) == D("0.585")


# ------------------------------------------------------------------------------- supply
def test_supply_generation_only() -> None:
    s = supply_terms(inputs(cell(ghi="800", temp="30")), P)
    assert s.generation_kw == D("2.8288")
    assert s.weighted_generation_kw == D("2.8288")  # w_gen = 1.0
    assert s.supply_kw == D("2.8288")
    # S_min = max(0.1, 0.10 * 5 kW) = 0.5; supply is above it
    assert s.s_min_kw == D("0.5") and not s.floor_applied
    assert s.effective_kw == D("2.8288")


def test_supply_includes_tradeable_inventory() -> None:
    # 24 kWh spread over 12 h = 2 kW; weighted by w_inv = 0.5 -> 1 kW
    s = supply_terms(inputs(cell(ghi="0"), inventory_wh=24_000), P)
    assert s.inventory_kw == D(2)
    assert s.weighted_inventory_kw == D(1)
    assert s.supply_kw == D(1) and s.effective_kw == D(1) and not s.floor_applied


def test_supply_sums_generation_and_inventory() -> None:
    s = supply_terms(inputs(cell(ghi="800", temp="30"), inventory_wh=24_000), P)
    assert s.supply_kw == D("3.8288")  # 2.8288 + 1.0


def test_supply_floor_prevents_division_by_zero() -> None:
    s = supply_terms(inputs(cell(ghi="0")), P)  # night, nobody holds any WATT
    assert s.supply_kw == 0
    assert s.effective_kw == D("0.5") and s.floor_applied  # 10 % of 5 kW installed
    # for a tiny installation the absolute floor (0.1 kW) wins
    tiny = supply_terms(inputs(cell(capacity_w=100, ghi="0")), P)
    assert tiny.s_min_kw == D("0.1") and tiny.effective_kw == D("0.1")


def test_supply_sums_over_cells_with_their_own_weather() -> None:
    sunny = cell(5000, "800", "30", cell_id="a")
    cloudy = cell(3000, "200", "20", cell_id="b")
    # b: T_cell = 20 + 6 = 26 -> derate 1 - 0.004 = 0.996; 3 * 0.2 * 0.8 * 0.996 = 0.478080
    s = supply_terms(inputs(sunny, cloudy), P)
    assert s.generation_kw == D("2.8288") + D("0.47808")
    assert s.installed_kw == D(8)


# ------------------------------------------------------------------------------- demand
def test_demand_load_at_midnight() -> None:
    # local hour 0 -> multiplier 0.55; 0.55 * 0.35 * 5 kW = 0.9625 kW
    d = demand_terms(inputs(cell()), P)
    assert d.load_kw == D("0.9625")
    assert d.bids_kw == 0 and d.velocity_kw == 0
    assert d.demand_kw == D("0.9625") and d.profile_multiplier == D("0.55")


def test_demand_bids_and_velocity() -> None:
    # bids: 6 kWh over 1 h = 6 kW. velocity: 0.5 * 4 kWh / 1 h = 2 kW.
    d = demand_terms(inputs(cell(), bids_wh=6000, fills_wh=4000), P)
    assert d.bids_kw == D(6)
    assert d.velocity_kw == D(2)
    assert d.demand_kw == D("0.9625") + 8


def test_base_demand_does_not_scale_with_installed_capacity() -> None:
    # 2 kW of consumer demand x the 0.55 midnight multiplier = 1.1 kW on top of 0.9625 kW
    p = PricingParams(base_demand_kw=D(2))
    d = demand_terms(inputs(cell()), p)
    assert d.base_kw == D("1.1") and d.load_kw == D("0.9625") + D("1.1")
    # doubling the installed base doubles the proportional part only
    d2 = demand_terms(inputs(cell(10_000)), p)
    assert d2.base_kw == D("1.1") and d2.load_kw == D("1.925") + D("1.1")
    # with no meters at all the base demand is still there (at local solar time of lon 0)
    assert demand_terms(inputs(), p).demand_kw == D("1.1")


def test_without_base_demand_added_meters_leave_the_raw_price_unchanged() -> None:
    """Documented property of the default model: demand and generation both scale with capacity."""

    def raw(cap_w: int, p: PricingParams) -> int:
        i = inputs(cell(cap_w, ghi="800", temp="30"))
        return raw_price(supply_terms(i, p), demand_terms(i, p), p).price

    assert raw(5_000, P) == raw(50_000, P)
    with_base = PricingParams(base_demand_kw=D(2))
    assert raw(50_000, with_base) < raw(5_000, with_base)  # more supply, same consumers: cheaper


def test_demand_uses_each_cells_local_time() -> None:
    # Same UTC instant (00:00): a cell at 0 deg is at local midnight (0.55);
    # a cell at 180 deg is at local noon (0.70). Equal capacities.
    west = cell(4000, cell_id="w", lon_e6=0)
    east = cell(4000, cell_id="e", lon_e6=180_000_000 - 1)  # ~ 12:00 local
    d = demand_terms(inputs(west, east), P)
    assert d.load_kw > D("0.55") * D("0.35") * 8  # more than all-night demand
    assert d.load_kw < D("0.70") * D("0.35") * 8 + D("0.001")


# --------------------------------------------------------------------------- raw price
def test_raw_price_inventory_driven_night() -> None:
    # night, 24 kWh held: S_eff = 1.0, D = 0.9625 -> ratio 0.9625
    # P = 150000 * 0.9625 ** 0.6 = 146 599 (ln 0.9625 = -0.038221, * 0.6 = -0.022933, e^ = 0.977328)
    i = inputs(cell(ghi="0"), inventory_wh=24_000)
    r = raw_price(supply_terms(i, P), demand_terms(i, P), P)
    assert r.ratio == D("0.9625") and not r.ratio_capped and not r.neutral
    assert r.price == 146_599 and r.clamp is None


def test_raw_price_night_without_inventory_is_sane_not_the_ceiling() -> None:
    # S_eff = S_min = 0.5, D = 0.9625 -> ratio 1.925 -> 150000 * 1.925 ** 0.6 = 222 203
    i = inputs(cell(ghi="0"))
    r = raw_price(supply_terms(i, P), demand_terms(i, P), P)
    assert r.price == 222_203
    assert P.p_base < r.price < P.p_ceiling // 2  # elevated (scarcity), nowhere near the ceiling


def test_raw_price_noon() -> None:
    # GHI 900, 30 C: T_cell 57, derate 0.872, gen = 5 * 0.9 * 0.8 * 0.872 = 3.1392 kW;
    # + 1.0 kW inventory = 4.1392; D = 0.70 * 0.35 * 5 = 1.225; ratio = 0.295955...
    i = inputs(cell(ghi="900", temp="30"), t=MIDNIGHT + 12 * 3600, inventory_wh=24_000)
    s, d = supply_terms(i, P), demand_terms(i, P)
    assert s.generation_kw == D("3.1392") and s.effective_kw == D("4.1392")
    assert d.demand_kw == D("1.225")
    assert raw_price(s, d, P).price == 72_248  # float cross-check: 72247.54


def test_raw_price_ratio_cap() -> None:
    # 100 kWh of open bids in an hour: D ~ 101 kW against S_eff 0.5 -> ratio capped at 10
    # P = 150000 * 10 ** 0.6 = 597 161
    i = inputs(cell(ghi="0"), bids_wh=100_000)
    r = raw_price(supply_terms(i, P), demand_terms(i, P), P)
    assert r.ratio_capped and r.ratio == 10 and r.ratio_uncapped > 100
    assert r.price == 597_161 and r.clamp is None


def test_raw_price_ceiling_and_floor_clamps() -> None:
    i = inputs(cell(ghi="0"), bids_wh=100_000)
    low_ceiling = PricingParams(p_ceiling=500_000)
    r = raw_price(supply_terms(i, low_ceiling), demand_terms(i, low_ceiling), low_ceiling)
    assert r.price == 500_000 and r.clamp == "ceiling" and r.unclamped > 500_000

    # lots of supply: 1000 kWh of inventory -> 83.3 kW * 0.5 = 41.7 kW against ~1 kW of demand
    flooded = inputs(cell(ghi="0"), inventory_wh=1_000_000)
    r2 = raw_price(supply_terms(flooded, P), demand_terms(flooded, P), P)
    assert r2.price == P.p_floor and r2.clamp == "floor"


def test_raw_price_neutral_when_the_market_is_empty() -> None:
    i = inputs()  # no meters, no inventory, no bids
    r = raw_price(supply_terms(i, P), demand_terms(i, P), P)
    assert r.neutral and r.price == P.p_base


def test_elasticity_closed_forms() -> None:
    # epsilon = 1: P = base * ratio. epsilon = 0.5: P = base * sqrt(ratio).
    i = inputs(cell(ghi="0"), inventory_wh=24_000)  # ratio 0.9625
    s, d = supply_terms(i, P), demand_terms(i, P)
    linear = PricingParams(epsilon=D(1))
    assert raw_price(s, d, linear).price == 144_375  # 150000 * 0.9625
    root = PricingParams(epsilon=D("0.5"))
    assert raw_price(s, d, root).price == 147_161  # 150000 * sqrt(0.9625) = 147 160.6


@pytest.mark.parametrize("ratio_inputs", [(0, 0), (100, 0), (0, 5000)])
def test_raw_price_is_always_an_int_in_range(ratio_inputs: tuple[int, int]) -> None:
    bids, inv = ratio_inputs
    i = inputs(cell(ghi="500"), bids_wh=bids, inventory_wh=inv)
    r = raw_price(supply_terms(i, P), demand_terms(i, P), P)
    assert isinstance(r.price, int) and P.p_floor <= r.price <= P.p_ceiling
