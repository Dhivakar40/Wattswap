"""The pricing model: supply, demand and the raw price. Pure functions, Decimal arithmetic.

Units. Power is in kW (average over the stated horizon); energy inputs are Wh (WATT base
units, 1 WATT = 1 Wh * 1e-3 decimals). The only output that leaves this module as money is an
integer number of mUSDC base units (6 decimals) per kWh.

See docs/pricing-model.md for the derivation and worked examples.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import ROUND_HALF_EVEN, Context, Decimal

from app.pricing.params import PricingParams

D = Decimal
ZERO = D(0)
ONE = D(1)
THOUSAND = D(1000)
CTX = Context(prec=28, rounding=ROUND_HALF_EVEN)  # explicit: results never depend on ambient state


# ---------------------------------------------------------------------------- inputs
@dataclass(frozen=True)
class CellInput:
    """One grid cell: the installed capacity inside it and the weather valid for it."""

    cell_id: str
    capacity_w: int  # sum of rated capacities of active, registered meters in the cell
    meters: int
    center_lon_e6: int
    ghi_wm2: Decimal  # shortwave_radiation
    temp_c: Decimal
    cloud_cover_pct: Decimal | None = None
    weather_age_s: int = 0  # wall-clock seconds since this weather was fetched
    weather_source: str = "live"  # live | last_good | archive | none


@dataclass(frozen=True)
class MarketInputs:
    market_time: int  # unix seconds on the MARKET clock (may be simulated)
    cells: tuple[CellInput, ...]
    inventory_wh: int = 0  # tradeable inventory
    inventory_source: str = "none"
    open_bid_wh: int = 0
    recent_fill_wh: int = 0
    onchain_band_bps: int = 500


# ----------------------------------------------------------------------- physical pieces
def cell_temperature(temp_c: Decimal, ghi_wm2: Decimal, rise: Decimal) -> Decimal:
    """T_cell = T_air + rise * GHI."""
    return temp_c + rise * ghi_wm2


def temp_derate(cell_temp_c: Decimal, coeff: Decimal) -> Decimal:
    """derate = 1 - coeff * max(0, T_cell - 25): cold cells are not boosted above nameplate."""
    return ONE - coeff * max(ZERO, cell_temp_c - D(25))


def generation_kw(capacity_w: int, ghi_wm2: Decimal, temp_c: Decimal, p: PricingParams) -> Decimal:
    """capacity_kW * (GHI / 1000) * PR * derate."""
    cap_kw = D(capacity_w) / THOUSAND
    t_cell = cell_temperature(temp_c, ghi_wm2, p.cell_temp_rise)
    return max(
        ZERO,
        cap_kw * (ghi_wm2 / THOUSAND) * p.performance_ratio * temp_derate(t_cell, p.temp_coeff),
    )


def local_solar_hour(ts: int, lon_e6: int) -> Decimal:
    """Local mean solar time in hours [0, 24): UTC hour plus longitude / 15 (no tz database)."""
    utc_hours = D(ts % 86_400) / D(3600)
    # Decimal % keeps the dividend's sign (like C fmod), so normalise into [0, 24) explicitly.
    return ((utc_hours + D(lon_e6) / D(15_000_000)) % D(24) + D(24)) % D(24)


def load_multiplier(profile: Sequence[Decimal], hour: Decimal) -> Decimal:
    """Linear interpolation of the 24-point profile at fractional hour `hour`, wrapping at 24."""
    lo = int(hour)  # hour is in [0, 24)
    frac = hour - lo
    a, b = profile[lo % 24], profile[(lo + 1) % 24]
    return a + (b - a) * frac


# --------------------------------------------------------------------------- supply side
@dataclass(frozen=True)
class SupplyTerms:
    generation_kw: Decimal  # forecast generation, summed over cells
    inventory_kw: Decimal  # tradeable inventory spread over the inventory horizon
    weighted_generation_kw: Decimal  # w_gen * generation
    weighted_inventory_kw: Decimal  # w_inv * inventory
    supply_kw: Decimal  # S = weighted generation + weighted inventory
    s_min_kw: Decimal  # the floor actually in force
    effective_kw: Decimal  # S_eff = max(S, S_min)
    floor_applied: bool
    installed_kw: Decimal


def supply_terms(inputs: MarketInputs, p: PricingParams) -> SupplyTerms:
    installed_kw = sum((D(c.capacity_w) / THOUSAND for c in inputs.cells), ZERO)
    gen = sum((generation_kw(c.capacity_w, c.ghi_wm2, c.temp_c, p) for c in inputs.cells), ZERO)
    inv = D(inputs.inventory_wh) / THOUSAND / p.inventory_horizon_hours
    wg, wi = p.w_gen * gen, p.w_inv * inv
    s = wg + wi
    s_min = max(p.s_min_kw, p.s_min_frac * installed_kw)
    return SupplyTerms(gen, inv, wg, wi, s, s_min, max(s, s_min), s < s_min, installed_kw)


# --------------------------------------------------------------------------- demand side
@dataclass(frozen=True)
class DemandTerms:
    load_kw: Decimal  # base_demand_kw x the time-of-day load profile (independent of installed PV)
    bids_kw: Decimal  # open bid volume spread over the bid horizon
    velocity_kw: Decimal  # recent fills, weighted
    demand_kw: Decimal  # D
    profile_multiplier: Decimal  # capacity-weighted mean of the cells' multipliers (for display)
    local_hour: Decimal  # capacity-weighted mean local solar hour (for display)


def demand_terms(inputs: MarketInputs, p: PricingParams) -> DemandTerms:
    """D = base_demand_kw x profile(local solar hour) + open bids + fill velocity.

    Demand does NOT depend on the installed capacity: consumers exist whether or not there are
    panels. The profile's local hour is the capacity-weighted mean over the market's cells (or
    the hour at longitude 0 when the market has no meters yet).
    """
    weighted_mult = ZERO
    weighted_hour = ZERO
    installed = ZERO
    for c in inputs.cells:
        cap_kw = D(c.capacity_w) / THOUSAND
        hour = local_solar_hour(inputs.market_time, c.center_lon_e6)
        mult = load_multiplier(p.load_profile, hour)
        weighted_mult += mult * cap_kw
        weighted_hour += hour * cap_kw
        installed += cap_kw
    mult_mean = (
        weighted_mult / installed
        if installed > 0
        else load_multiplier(p.load_profile, local_solar_hour(inputs.market_time, 0))
    )
    hour_mean = (
        weighted_hour / installed if installed > 0 else local_solar_hour(inputs.market_time, 0)
    )
    load = p.base_demand_kw * mult_mean
    bids = D(inputs.open_bid_wh) / THOUSAND / p.bid_horizon_hours
    velocity = p.velocity_weight * D(inputs.recent_fill_wh) / THOUSAND / p.velocity_window_hours
    return DemandTerms(load, bids, velocity, load + bids + velocity, mult_mean, hour_mean)


# ------------------------------------------------------------------------------ raw price
@dataclass(frozen=True)
class RawPrice:
    ratio_uncapped: Decimal
    ratio: Decimal
    ratio_capped: bool
    neutral: bool  # nothing in the market at all: price is p_base
    unclamped: Decimal  # p_base * ratio ** epsilon, before the floor/ceiling
    price: int  # clamped, quantized: mUSDC base units per kWh
    clamp: str | None  # "floor" | "ceiling" | None


def raw_price(s: SupplyTerms, d: DemandTerms, p: PricingParams) -> RawPrice:
    """P_raw = P_base * (D / S_eff) ** epsilon, ratio capped, clamped to [floor, ceiling]."""
    # Nobody on either side of the market (no meters, no inventory, no bids, no fills): there is
    # nothing to price, so quote the neutral base price rather than a scarcity price.
    neutral = s.installed_kw == 0 and s.supply_kw == 0 and d.bids_kw == 0 and d.velocity_kw == 0
    if neutral:
        ratio_uncapped = ratio = ONE
        capped = False
    else:
        ratio_uncapped = d.demand_kw / s.effective_kw
        capped = ratio_uncapped > p.ratio_cap
        ratio = min(ratio_uncapped, p.ratio_cap)
    unclamped = D(p.p_base) * CTX.power(ratio, p.epsilon) if ratio > 0 else ZERO
    quantized = int(unclamped.quantize(ONE, rounding=ROUND_HALF_EVEN))
    clamp: str | None = None
    price = quantized
    if quantized < p.p_floor:
        price, clamp = p.p_floor, "floor"
    elif quantized > p.p_ceiling:
        price, clamp = p.p_ceiling, "ceiling"
    return RawPrice(ratio_uncapped, ratio, capped, neutral, unclamped, price, clamp)
