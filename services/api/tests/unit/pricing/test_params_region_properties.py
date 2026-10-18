"""Parameter validation, grid cells, and whole-engine properties."""

from decimal import Decimal
from itertools import pairwise

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from app.pricing.engine import EngineState, step
from app.pricing.model import demand_terms, raw_price, supply_terms
from app.pricing.params import (
    DEFAULT_LOAD_PROFILE,
    SPECS,
    ParamError,
    PricingParams,
    from_mapping,
    max_applied_move_bps,
)
from app.pricing.region import BoundingBox, Cell, cell_from_id, cell_of
from tests.unit.pricing.helpers import MIDNIGHT, cell, inputs

D = Decimal
P = PricingParams()


# ------------------------------------------------------------------------------- params
def test_defaults_are_the_approved_ones() -> None:
    assert (P.p_base, P.p_floor, P.p_ceiling) == (150_000, 20_000, 600_000)
    assert (P.epsilon, P.ema_alpha, P.max_step_bps) == (D("0.6"), D("0.2"), 25)
    assert (P.weather_stale_after_s, P.move_bps, P.move_window_s) == (1800, 1500, 600)
    assert len(DEFAULT_LOAD_PROFILE) == 24 and P.tick_seconds == 15
    assert set(SPECS) == set(P.to_mapping())  # every parameter is documented and bounded


def test_overrides_apply_and_roundtrip() -> None:
    q = from_mapping({"p_base": 160_000, "epsilon": "0.5", "halt_quotes_on_freeze": True})
    assert q.p_base == 160_000 and q.epsilon == D("0.5") and q.halt_quotes_on_freeze
    assert from_mapping(q.to_mapping()) == q  # to_mapping is lossless


@pytest.mark.parametrize(
    ("bad", "key"),
    [
        ({"nope": 1}, "nope"),
        ({"p_base": 0}, "p_base"),
        ({"p_base": "abc"}, "p_base"),
        ({"p_base": 150_000.5}, "p_base"),  # floats are refused outright
        ({"epsilon": 0.6}, "epsilon"),  # ... including 0.6 as a float
        ({"epsilon": "10"}, "epsilon"),
        ({"ema_alpha": "0"}, "ema_alpha"),
        ({"ema_alpha": "1.5"}, "ema_alpha"),
        ({"max_step_bps": 0}, "max_step_bps"),
        ({"halt_quotes_on_freeze": "yes"}, "halt_quotes_on_freeze"),
        ({"load_profile": ["1"] * 23}, "load_profile"),
        ({"load_profile": ["99"] * 24}, "load_profile"),
        ({"p_ceiling": 100_000}, "p_base"),  # floor < base < ceiling
        ({"p_floor": 200_000}, "p_base"),
        ({"w_gen": 0, "w_inv": 0}, "w_gen"),
        ({"epsilon": "NaN"}, "epsilon"),
    ],
)
def test_invalid_overrides_are_rejected_with_the_offending_key(
    bad: dict[str, object], key: str
) -> None:
    with pytest.raises(ParamError) as exc:
        from_mapping(bad)
    assert key in exc.value.errors


def test_the_limiter_must_stay_below_the_breaker_threshold() -> None:
    # 26 bps/tick over 40 ticks is still < 15 %, 60 is not: (1.006)^40 - 1 = 27 %
    assert from_mapping({"max_step_bps": 30}).max_step_bps == 30
    with pytest.raises(ParamError) as exc:
        from_mapping({"max_step_bps": 60})
    assert "limiter alone could move the price" in exc.value.errors["max_step_bps"]
    # the boundary is exact for the defaults: find the largest accepted step
    accepted = max(s for s in range(1, 200) if max(max_applied_move_bps(s, 40)) < 1500)
    assert from_mapping({"max_step_bps": accepted}).max_step_bps == accepted
    with pytest.raises(ParamError):
        from_mapping({"max_step_bps": accepted + 1})
    # a shorter breaker window makes the rule easier, a longer one harder
    assert from_mapping({"move_window_s": 60, "max_step_bps": 100}).move_window_s == 60
    with pytest.raises(ParamError):
        from_mapping({"move_window_s": 3600})  # 240 ticks at 25 bps = +82 %


def test_a_floor_too_low_to_step_is_rejected() -> None:
    with pytest.raises(ParamError):
        from_mapping({"p_floor": 9999})  # below the bound that guarantees a >= 1 unit step


def test_max_applied_move_closed_forms() -> None:
    assert max_applied_move_bps(25, 40) == (1051, 953)
    assert max_applied_move_bps(100, 1) == (100, 100)
    assert max_applied_move_bps(25, 0) == (0, 0)


# ------------------------------------------------------------------------------ regions
def test_cells_use_integer_floor_division() -> None:
    # Chennai 13.0827 N, 80.2707 E -> (26, 160); centre 13.25 N, 80.25 E
    c = cell_of(13_082_700, 80_270_700)
    assert (c.ilat, c.ilon) == (26, 160) and c.id == "26:160"
    assert (c.center_lat, c.center_lon) == (D("13.25"), D("80.25"))
    # negatives floor toward -inf: -33.8688 S, 151.2093 E (Sydney) -> (-68, 302)
    s = cell_of(-33_868_800, 151_209_300)
    assert (s.ilat, s.ilon) == (-68, 302)
    assert s.center_lat == D("-33.75")
    # the cell boundary itself belongs to the upper cell
    assert cell_of(500_000, 500_000) == Cell(1, 1) and cell_of(499_999, 499_999) == Cell(0, 0)
    assert cell_from_id("-68:302") == s


def test_nearby_meters_share_one_cell() -> None:
    assert cell_of(13_082_700, 80_270_700) == cell_of(13_000_001, 80_000_001)
    assert cell_of(13_082_700, 80_270_700) != cell_of(13_082_700, 80_600_000)


def test_bounding_box() -> None:
    everywhere = BoundingBox()
    assert everywhere.contains(-89_000_000, 179_000_000)
    india = BoundingBox(6_000_000, 36_000_000, 68_000_000, 98_000_000)
    assert india.contains(13_082_700, 80_270_700)
    assert not india.contains(-33_868_800, 151_209_300)
    assert india.contains(6_000_000, 68_000_000) and not india.contains(5_999_999, 80_000_000)


# --------------------------------------------------------------------------- properties
cells_st = st.builds(
    lambda cap, ghi, temp, lon: cell(cap, str(ghi), str(temp), lon_e6=lon),
    st.integers(500, 200_000),
    st.integers(0, 1300),
    st.integers(-30, 55),
    st.integers(-179_000_000, 179_000_000),
)


@settings(max_examples=150, deadline=None)
@given(
    cells=st.lists(cells_st, min_size=0, max_size=4),
    inventory=st.integers(0, 5_000_000),
    bids=st.integers(0, 5_000_000),
    fills=st.integers(0, 5_000_000),
    t=st.integers(0, 86_399),
)
def test_price_is_always_a_bounded_int(
    cells: list[object], inventory: int, bids: int, fills: int, t: int
) -> None:
    i = inputs(*cells, t=MIDNIGHT + t, inventory_wh=inventory, bids_wh=bids, fills_wh=fills)  # type: ignore[arg-type]
    _, tick = step(EngineState(), i, P, MIDNIGHT + t)
    assert isinstance(tick.price, int) and isinstance(tick.raw_price, int)
    assert P.p_floor <= tick.price <= P.p_ceiling
    assert P.p_floor <= tick.raw_price <= P.p_ceiling
    assert tick.band_bps <= i.onchain_band_bps


@settings(max_examples=100, deadline=None)
@given(
    capacity=st.integers(1000, 100_000),
    ghi=st.integers(0, 1200),
    inventory=st.integers(0, 1_000_000),
    extra_inventory=st.integers(1, 1_000_000),
    extra_ghi=st.integers(1, 200),
    bids=st.integers(0, 100_000),
    extra_bids=st.integers(1, 100_000),
)
def test_more_supply_never_raises_and_more_demand_never_lowers_the_raw_price(
    capacity: int,
    ghi: int,
    inventory: int,
    extra_inventory: int,
    extra_ghi: int,
    bids: int,
    extra_bids: int,
) -> None:
    def raw(inv: int, g: int, b: int) -> int:
        i = inputs(cell(capacity, str(g), "25"), inventory_wh=inv, bids_wh=b)
        return raw_price(supply_terms(i, P), demand_terms(i, P), P).price

    base = raw(inventory, ghi, bids)
    assert raw(inventory + extra_inventory, ghi, bids) <= base
    assert raw(inventory, min(ghi + extra_ghi, 1300), bids) <= base
    assert raw(inventory, ghi, bids + extra_bids) >= base


@settings(max_examples=40, deadline=None)
@given(bids=st.lists(st.integers(0, 3_000_000), min_size=60, max_size=120))
def test_engine_steps_never_exceed_the_limit_whatever_the_market_does(bids: list[int]) -> None:
    state = EngineState()
    prices: list[int] = []
    for n, b in enumerate(bids):
        state, tick = step(
            state, inputs(cell(ghi="0"), inventory_wh=24_000, bids_wh=b), P, MIDNIGHT + n * 15
        )
        prices.append(tick.price)
    for a, b in pairwise(prices):
        assert abs(b - a) * 10_000 <= a * P.max_step_bps
    window = P.move_window_s // P.tick_seconds
    for i in range(len(prices) - window):
        assert abs(prices[i + window] - prices[i]) * 10_000 < P.move_bps * prices[i]
