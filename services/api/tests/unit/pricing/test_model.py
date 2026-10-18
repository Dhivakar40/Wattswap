"""Every model formula against a hand-computed value.

The derivations are in the comments so a reviewer can redo them with a calculator.
"""

from decimal import Decimal

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

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
def test_demand_is_base_demand_times_the_profile() -> None:
    # local hour 0 -> multiplier 0.55; 4.5 kW x 0.55 = 2.475 kW
    d = demand_terms(inputs(cell()), P)
    assert d.load_kw == D("2.475")
    assert d.bids_kw == 0 and d.velocity_kw == 0
    assert d.demand_kw == D("2.475") and d.profile_multiplier == D("0.55")


def test_demand_bids_and_velocity() -> None:
    # bids: 6 kWh over 1 h = 6 kW. velocity: 0.5 * 4 kWh / 1 h = 2 kW.
    d = demand_terms(inputs(cell(), bids_wh=6000, fills_wh=4000), P)
    assert d.bids_kw == D(6)
    assert d.velocity_kw == D(2)
    assert d.demand_kw == D("2.475") + 8


def test_demand_does_not_depend_on_installed_capacity() -> None:
    small = demand_terms(inputs(cell(5_000)), P)
    big = demand_terms(inputs(cell(50_000)), P)
    assert small.demand_kw == big.demand_kw == D("2.475")
    # it scales with base_demand_kw and the profile instead: 2 kW x 0.55 = 1.1 kW
    assert demand_terms(inputs(cell()), PricingParams(base_demand_kw=D(2))).demand_kw == D("1.1")
    # with no meters at all the demand is still there (profile at the local solar time of lon 0)
    assert demand_terms(inputs(), PricingParams(base_demand_kw=D(2))).demand_kw == D("1.1")
    assert demand_terms(
        inputs(t=MIDNIGHT + 12 * 3600), PricingParams(base_demand_kw=D(2))
    ).demand_kw == D("1.4")


def test_zero_base_demand_leaves_only_the_order_book() -> None:
    p = PricingParams(base_demand_kw=D(0))
    assert demand_terms(inputs(cell()), p).demand_kw == 0
    assert demand_terms(inputs(cell(), bids_wh=3000), p).demand_kw == D(3)


def test_demand_profile_uses_the_capacity_weighted_local_time_of_the_cells() -> None:
    # Same UTC instant (00:00): a cell at 0 deg is at local midnight (0.55); a cell at ~180 deg is
    # at local noon (0.70). Equal capacities -> mean multiplier 0.625 -> 4.5 x 0.625 = 2.8125 kW.
    west = cell(4000, cell_id="w", lon_e6=0)
    east = cell(4000, cell_id="e", lon_e6=180_000_000 - 1)
    d = demand_terms(inputs(west, east), P)
    assert abs(d.load_kw - D("2.8125")) < D("0.0001")
    # an installation 3x bigger in the west pulls the weighted time toward midnight
    lopsided = demand_terms(inputs(cell(12_000, cell_id="w", lon_e6=0), east), P)
    assert lopsided.load_kw < d.load_kw


# --------------------------------------------------------------------------- raw price
def test_raw_price_inventory_driven_night() -> None:
    # night, 24 kWh held: S_eff = 1.0, D = 2.475 -> ratio 2.475
    # P = 150000 * 2.475 ** 0.6 = 258 367 (ln 2.475 = 0.906, * 0.6 = 0.5436, e^ = 1.7224)
    i = inputs(cell(ghi="0"), inventory_wh=24_000)
    r = raw_price(supply_terms(i, P), demand_terms(i, P), P)
    assert r.ratio == D("2.475") and not r.ratio_capped and not r.neutral
    assert r.price == 258_367 and r.clamp is None


def test_raw_price_night_without_inventory_is_scarce_but_below_the_ceiling() -> None:
    # S_eff = S_min = 0.5, D = 2.475 -> ratio 4.95 -> 150000 * 4.95 ** 0.6 = 391 611
    i = inputs(cell(ghi="0"))
    r = raw_price(supply_terms(i, P), demand_terms(i, P), P)
    assert r.price == 391_611 and not r.ratio_capped and r.clamp is None
    assert P.p_base < r.price < P.p_ceiling  # scarcity pricing, not the ceiling
    # stocking the market with WATT brings it down (inventory-driven)
    stocked = inputs(cell(ghi="0"), inventory_wh=24_000)
    assert raw_price(supply_terms(stocked, P), demand_terms(stocked, P), P).price < r.price


def test_raw_price_noon() -> None:
    # GHI 900, 30 C: T_cell 57, derate 0.872, gen = 5 * 0.9 * 0.8 * 0.872 = 3.1392 kW;
    # + 1.0 kW inventory = 4.1392; D = 4.5 * 0.70 = 3.15; ratio = 0.76099...
    # P = 150000 * 0.76099 ** 0.6 = 127 329 (float cross-check: 127329.12)
    i = inputs(cell(ghi="900", temp="30"), t=MIDNIGHT + 12 * 3600, inventory_wh=24_000)
    s, d = supply_terms(i, P), demand_terms(i, P)
    assert s.generation_kw == D("3.1392") and s.effective_kw == D("4.1392")
    assert d.demand_kw == D("3.15")
    assert raw_price(s, d, P).price == 127_329


def test_raw_price_ratio_cap() -> None:
    # 100 kWh of open bids in an hour: D ~ 107 kW against S_eff 0.5 -> ratio capped at 10
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

    # lots of supply: 2000 kWh of inventory -> 166.7 kW * 0.5 = 83.3 kW against 2.475 kW of demand
    # ratio 0.0297 -> 150000 * 0.0297 ** 0.6 = 18 186, below the 20 000 floor
    flooded = inputs(cell(ghi="0"), inventory_wh=2_000_000)
    r2 = raw_price(supply_terms(flooded, P), demand_terms(flooded, P), P)
    assert r2.price == P.p_floor and r2.clamp == "floor" and 18_000 < r2.unclamped < 18_400


def test_raw_price_neutral_only_when_nobody_is_in_the_market() -> None:
    i = inputs()  # no meters, no inventory, no bids, no fills
    r = raw_price(supply_terms(i, P), demand_terms(i, P), P)
    assert r.neutral and r.price == P.p_base
    # the moment there is any order-book activity it is a real market again
    busy = inputs(bids_wh=1000)
    assert not raw_price(supply_terms(busy, P), demand_terms(busy, P), P).neutral


def test_elasticity_closed_forms() -> None:
    # epsilon = 1: P = base * ratio. epsilon = 0.5: P = base * sqrt(ratio).
    i = inputs(cell(ghi="0"), inventory_wh=24_000)  # ratio 2.475
    s, d = supply_terms(i, P), demand_terms(i, P)
    linear = PricingParams(epsilon=D(1))
    assert raw_price(s, d, linear).price == 371_250  # 150000 * 2.475
    root = PricingParams(epsilon=D("0.5"))
    assert raw_price(s, d, root).price == 235_982  # 150000 * sqrt(2.475) = 235 981.99


# ------------------------------------------------- the market reacts to supply
NOON = MIDNIGHT + 12 * 3600


def noon_raw(capacity_w: int, p: PricingParams = P, inventory_wh: int = 0) -> int:
    """Raw price at local solar noon on a clear day (GHI 900, 30 C), for one cell of this size."""
    i = inputs(cell(capacity_w, ghi="900", temp="30"), t=NOON, inventory_wh=inventory_wh)
    return raw_price(supply_terms(i, p), demand_terms(i, p), p).price


def test_adding_capacity_lowers_the_noon_raw_price_and_removing_it_raises_it() -> None:
    fleet = [5_000, 10_000, 15_000, 20_000, 40_000]
    prices = [noon_raw(c) for c in fleet]
    # hand-computed: S = cap_kW * 0.9 * 0.8 * 0.872 = 0.62784 * cap_kW against D = 3.15 kW
    assert prices == [150_309, 99_167, 77_752, 65_426, 43_165]
    assert prices == sorted(prices, reverse=True)  # strictly cheaper with every added meter
    # ... and back up again as capacity is removed
    for bigger, smaller in zip(fleet[1:], fleet, strict=False):
        assert noon_raw(smaller) > noon_raw(bigger)
    assert noon_raw(5_000) == prices[0]  # removing the meters restores the original price exactly


def test_demand_is_unchanged_when_capacity_changes() -> None:
    a = demand_terms(inputs(cell(5_000, ghi="900"), t=NOON), P)
    b = demand_terms(inputs(cell(40_000, ghi="900"), t=NOON), P)
    assert a.demand_kw == b.demand_kw == D("3.15")  # same demand; only supply moved


@settings(max_examples=100, deadline=None)
@given(
    capacity=st.integers(1_000, 200_000),
    extra=st.integers(1_000, 200_000),
    inventory=st.integers(0, 200_000),
)
def test_more_capacity_never_raises_the_noon_price(
    capacity: int, extra: int, inventory: int
) -> None:
    assert noon_raw(capacity + extra, inventory_wh=inventory) <= noon_raw(
        capacity, inventory_wh=inventory
    )


def test_calibration_the_demo_fleet_prices_close_to_p_base_at_noon() -> None:
    """base_demand_kw = 4.5 is calibrated on the demo fleet: one 5 kW meter, clear noon, no WATT held."""
    price = noon_raw(5_000)
    assert price == 150_309  # 0.21 % above P_base
    assert abs(price - P.p_base) * 200 < P.p_base  # within 0.5 %
    # the exact calibrating value would be 3.1392 kW / 0.70 = 4.48457 kW
    assert abs(D("3.1392") / D("0.70") - P.base_demand_kw) < D("0.02")
    exact = PricingParams(base_demand_kw=D("3.1392") / D("0.70"))
    assert abs(noon_raw(5_000, exact) - P.p_base) <= 1


@pytest.mark.parametrize("ratio_inputs", [(0, 0), (100, 0), (0, 5000)])
def test_raw_price_is_always_an_int_in_range(ratio_inputs: tuple[int, int]) -> None:
    bids, inv = ratio_inputs
    i = inputs(cell(ghi="500"), bids_wh=bids, inventory_wh=inv)
    r = raw_price(supply_terms(i, P), demand_terms(i, P), P)
    assert isinstance(r.price, int) and P.p_floor <= r.price <= P.p_ceiling
