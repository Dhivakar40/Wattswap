"""Pricing parameters: typed, validated, with documented bounds.

Everything here is plain data (ints and Decimals). Prices are integers in mUSDC base units
(6 decimals) per kWh, exactly the unit the contracts use: 150000 = 0.150000 mUSDC/kWh.
"""

from collections.abc import Mapping
from dataclasses import dataclass, field, fields, replace
from decimal import Decimal, InvalidOperation
from typing import Any

D = Decimal

# Hourly demand multipliers by LOCAL SOLAR hour (index 0 = 00:00). Linear between hours.
DEFAULT_LOAD_PROFILE: tuple[Decimal, ...] = tuple(
    D(x)
    for x in (
        "0.55", "0.52", "0.50", "0.50", "0.52", "0.60",  # 00-05 night
        "0.80", "0.95", "0.90", "0.80", "0.72", "0.70",  # 06-11 morning peak, then midday
        "0.70", "0.68", "0.68", "0.72", "0.80", "0.95",  # 12-17
        "1.10", "1.15", "1.05", "0.90", "0.75", "0.62",  # 18-23 evening peak
    )
)  # fmt: skip


# Set by the service from its settings, not through the admin API.
READONLY: frozenset[str] = frozenset({"tick_seconds"})


class ParamError(ValueError):
    """Invalid parameters. `errors` maps parameter name to a message."""

    def __init__(self, errors: dict[str, str]) -> None:
        super().__init__("; ".join(f"{k}: {v}" for k, v in errors.items()))
        self.errors = errors


@dataclass(frozen=True)
class PricingParams:
    # --- price (mUSDC base units per kWh) ---------------------------------------------
    p_base: int = 150_000
    p_floor: int = 20_000
    p_ceiling: int = 600_000
    epsilon: Decimal = D("0.6")  # elasticity: P_raw = P_base * (D / S_eff) ** epsilon

    # --- supply -----------------------------------------------------------------------
    performance_ratio: Decimal = D("0.80")
    cell_temp_rise: Decimal = D("0.03")  # T_cell = T_air + rise * GHI
    temp_coeff: Decimal = D("0.004")  # derate = 1 - coeff * max(0, T_cell - 25)
    w_gen: Decimal = D("1.0")  # weight of forecast generation
    w_inv: Decimal = D("0.5")  # weight of tradeable inventory
    inventory_horizon_hours: Decimal = D("12")  # inventory kWh -> kW: spread over this window
    s_min_kw: Decimal = D("0.1")  # absolute floor on effective supply
    s_min_frac: Decimal = D("0.10")  # ... and as a fraction of installed capacity (the larger wins)
    ratio_cap: Decimal = D("10")  # D / S_eff is capped here

    # --- demand -----------------------------------------------------------------------
    demand_scale: Decimal = D("0.35")  # load = profile * scale * installed capacity
    load_profile: tuple[Decimal, ...] = DEFAULT_LOAD_PROFILE
    bid_horizon_hours: Decimal = D("1")  # open bid kWh -> kW
    velocity_weight: Decimal = D("0.5")
    velocity_window_hours: Decimal = D("1")

    # --- smoothing --------------------------------------------------------------------
    ema_alpha: Decimal = D("0.2")
    max_step_bps: int = 25  # rate limiter: 0.25 % per tick

    # --- volatility -------------------------------------------------------------------
    vol_window: int = 40

    # --- circuit breaker --------------------------------------------------------------
    weather_stale_after_s: int = 1800  # freeze when any used cell's weather is older
    move_bps: int = 1500  # RAW price move that raises an anomaly (15 %) ...
    move_window_s: int = 600  # ... within this many seconds
    anomaly_hold_ticks: int = 40
    recovery_ticks: int = 8
    band_hint_bps: int = 150  # published band hint in normal operation
    band_widen_multiplier: Decimal = D("3")  # ... multiplied while anomalous/frozen
    halt_quotes_on_freeze: bool = False

    # --- cadence (also used by validation) --------------------------------------------
    tick_seconds: int = 15

    def with_overrides(self, overrides: Mapping[str, Any]) -> "PricingParams":
        return from_mapping(overrides, base=self, allow_readonly=True)

    def to_mapping(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for f in fields(self):
            v = getattr(self, f.name)
            if isinstance(v, Decimal):
                out[f.name] = str(v)
            elif isinstance(v, tuple):
                out[f.name] = [str(x) for x in v]
            else:
                out[f.name] = v
        return out


@dataclass(frozen=True)
class ParamSpec:
    kind: str  # int | decimal | bool | profile
    description: str
    minimum: Decimal | None = None
    maximum: Decimal | None = None
    unit: str = ""
    extra: dict[str, Any] = field(default_factory=dict)


SPECS: dict[str, ParamSpec] = {
    "p_base": ParamSpec(
        "int", "Neutral price when demand equals supply", D(1), D(10**12), "mUSDC µ/kWh"
    ),
    "p_floor": ParamSpec(
        "int",
        "Lowest price ever quoted (must allow a >= 1 unit step)",
        D(10_000),
        D(10**12),
        "mUSDC µ/kWh",
    ),
    "p_ceiling": ParamSpec("int", "Highest price ever quoted", D(1), D(10**12), "mUSDC µ/kWh"),
    "epsilon": ParamSpec(
        "decimal", "Price elasticity of the demand/supply ratio", D("0.05"), D("3")
    ),
    "performance_ratio": ParamSpec("decimal", "PV system performance ratio", D("0.3"), D("1")),
    "cell_temp_rise": ParamSpec(
        "decimal", "Cell temperature rise per W/m2 of irradiance", D(0), D("0.1"), "°C per W/m2"
    ),
    "temp_coeff": ParamSpec(
        "decimal", "Power loss per °C of cell temperature above 25 °C", D(0), D("0.02"), "per °C"
    ),
    "w_gen": ParamSpec("decimal", "Weight of forecast generation in effective supply", D(0), D(10)),
    "w_inv": ParamSpec("decimal", "Weight of tradeable inventory in effective supply", D(0), D(10)),
    "inventory_horizon_hours": ParamSpec(
        "decimal", "Window over which inventory kWh is spread to kW", D("0.5"), D(72), "h"
    ),
    "s_min_kw": ParamSpec(
        "decimal", "Absolute floor on effective supply", D("0.001"), D(10**6), "kW"
    ),
    "s_min_frac": ParamSpec(
        "decimal", "Floor on effective supply as a fraction of installed capacity", D(0), D(1)
    ),
    "ratio_cap": ParamSpec("decimal", "Cap on demand/supply", D(1), D(1000)),
    "demand_scale": ParamSpec(
        "decimal", "Baseline load as a fraction of installed capacity", D("0.01"), D(10)
    ),
    "load_profile": ParamSpec(
        "profile", "24 hourly demand multipliers by local solar hour", D(0), D(10)
    ),
    "bid_horizon_hours": ParamSpec(
        "decimal", "Open bid kWh are spread over this window", D("0.1"), D(72), "h"
    ),
    "velocity_weight": ParamSpec(
        "decimal", "Weight of recent fill velocity in demand", D(0), D(10)
    ),
    "velocity_window_hours": ParamSpec(
        "decimal", "Look-back window for fill velocity", D("0.1"), D(72), "h"
    ),
    "ema_alpha": ParamSpec("decimal", "EMA smoothing factor (1 = no smoothing)", D("0.01"), D(1)),
    "max_step_bps": ParamSpec("int", "Maximum price change per tick", D(1), D(500), "bps"),
    "vol_window": ParamSpec(
        "int", "Ticks in the realized-volatility window", D(5), D(10_000), "ticks"
    ),
    "weather_stale_after_s": ParamSpec(
        "int", "Weather older than this freezes repricing", D(300), D(86_400), "s"
    ),
    "move_bps": ParamSpec("int", "RAW price move that raises an anomaly", D(100), D(10_000), "bps"),
    "move_window_s": ParamSpec("int", "Window for the anomaly move test", D(60), D(86_400), "s"),
    "anomaly_hold_ticks": ParamSpec(
        "int", "Quiet ticks before an anomaly clears", D(1), D(10_000), "ticks"
    ),
    "recovery_ticks": ParamSpec(
        "int", "Healthy ticks required to leave FROZEN", D(1), D(10_000), "ticks"
    ),
    "band_hint_bps": ParamSpec(
        "int", "Published band hint in normal operation", D(1), D(10_000), "bps"
    ),
    "band_widen_multiplier": ParamSpec(
        "decimal", "Band hint multiplier while anomalous or frozen", D(1), D(20)
    ),
    "halt_quotes_on_freeze": ParamSpec(
        "bool", "Stop signing quotes while FROZEN (trading halts when they go stale)"
    ),
    "tick_seconds": ParamSpec("int", "Seconds between ticks", D(1), D(3600), "s"),
}


def _parse_decimal(key: str, raw: object) -> Decimal:
    if isinstance(raw, bool) or isinstance(raw, float):
        # Floats are rejected on purpose: a float carries binary rounding into a price.
        raise ParamError({key: "must be a decimal string or integer, not a float/bool"})
    try:
        value = D(str(raw))
    except InvalidOperation as exc:
        raise ParamError({key: f"not a number: {raw!r}"}) from exc
    if not value.is_finite():
        raise ParamError({key: "must be finite"})
    return value


def from_mapping(
    values: Mapping[str, Any], base: PricingParams | None = None, *, allow_readonly: bool = False
) -> PricingParams:
    """Apply `values` on top of `base`, validating types, ranges and cross-parameter rules."""
    base = base or PricingParams()
    errors: dict[str, str] = {}
    updates: dict[str, Any] = {}
    for key, raw in values.items():
        spec = SPECS.get(key)
        if spec is None:
            errors[key] = "unknown parameter"
            continue
        if key in READONLY and not allow_readonly:
            errors[key] = "read-only here; set PRICING_TICK_SECONDS in the environment"
            continue
        try:
            if spec.kind == "bool":
                if not isinstance(raw, bool):
                    raise ParamError({key: "must be true or false"})
                updates[key] = raw
                continue
            if spec.kind == "profile":
                if not isinstance(raw, list | tuple) or len(raw) != 24:
                    raise ParamError({key: "must be a list of exactly 24 values"})
                profile = tuple(_parse_decimal(key, x) for x in raw)
                for x in profile:
                    assert spec.minimum is not None and spec.maximum is not None
                    if not spec.minimum <= x <= spec.maximum:
                        raise ParamError(
                            {key: f"each value must be in [{spec.minimum}, {spec.maximum}]"}
                        )
                updates[key] = profile
                continue
            value = _parse_decimal(key, raw)
            if spec.kind == "int":
                if value != value.to_integral_value():
                    raise ParamError({key: "must be an integer"})
                value = value.to_integral_value()
            if spec.minimum is not None and value < spec.minimum:
                raise ParamError({key: f"must be >= {spec.minimum}"})
            if spec.maximum is not None and value > spec.maximum:
                raise ParamError({key: f"must be <= {spec.maximum}"})
            updates[key] = int(value) if spec.kind == "int" else value
        except ParamError as exc:
            errors.update(exc.errors)
    if errors:
        raise ParamError(errors)
    merged = replace(base, **updates)
    problems = cross_check(merged)
    if problems:
        raise ParamError(problems)
    return merged


def max_applied_move_bps(step_bps: int, ticks: int) -> tuple[int, int]:
    """Worst-case (up, down) change in the APPLIED price over `ticks` consecutive ticks, in bps,
    rounded up. Pure integer arithmetic: (1 + s)^n - 1 and 1 - (1 - s)^n with s = step_bps / 10000."""
    num_up, num_dn, den = 1, 1, 1
    for _ in range(ticks):
        num_up *= 10_000 + step_bps
        num_dn *= 10_000 - step_bps
        den *= 10_000
    up = -((-(num_up - den) * 10_000) // den)
    down = -((-(den - num_dn) * 10_000) // den)
    return up, down


def cross_check(p: PricingParams) -> dict[str, str]:
    """Rules that involve more than one parameter."""
    errors: dict[str, str] = {}
    if not p.p_floor < p.p_base < p.p_ceiling:
        errors["p_base"] = "must satisfy p_floor < p_base < p_ceiling"
    if p.p_floor * p.max_step_bps < 10_000:
        errors["max_step_bps"] = (
            "p_floor * max_step_bps / 10000 must allow a step of at least 1 unit"
        )
    if p.s_min_kw <= 0:
        errors["s_min_kw"] = "must be positive so demand/supply never divides by ~0"
    if p.w_gen + p.w_inv <= 0:
        errors["w_gen"] = "w_gen + w_inv must be positive"
    # The consistency rule between the limiter and the breaker: within one breaker window the
    # rate limiter alone must never be able to move the APPLIED price by the breaker threshold,
    # so the limiter's own movement can never look like an anomaly.
    ticks = -(-p.move_window_s // p.tick_seconds)
    up, down = max_applied_move_bps(p.max_step_bps, ticks)
    if max(up, down) >= p.move_bps:
        errors["max_step_bps"] = (
            f"limiter alone could move the price {max(up, down)} bps within the {p.move_window_s}s "
            f"breaker window ({ticks} ticks), which reaches move_bps={p.move_bps}"
        )
    return errors
