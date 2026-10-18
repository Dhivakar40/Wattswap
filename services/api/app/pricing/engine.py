"""The pricing engine step: a pure, deterministic function from (state, inputs) to (state, tick).

No I/O, no wall clock, no randomness, no floats. The same inputs always produce a byte-identical
tick, which is what makes the backtest and the unit tests meaningful.
"""

from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from decimal import Decimal, localcontext
from typing import Any

from app.pricing import breaker as brk
from app.pricing.breaker import BreakerState, Mode
from app.pricing.model import (
    CTX,
    CellInput,
    MarketInputs,
    cell_temperature,
    demand_terms,
    generation_kw,
    raw_price,
    supply_terms,
    temp_derate,
)
from app.pricing.params import PricingParams
from app.pricing.smoothing import clamp, ema, limit_step, quantize_price, realized_vol_bps

D = Decimal
DRIVERS_VERSION = 1


def ds(value: Decimal, places: int = 6) -> str:
    """Display string for a Decimal (fixed places). Never used for anything signed."""
    return str(value.quantize(D(1).scaleb(-places)))


@dataclass(frozen=True)
class EngineState:
    last_price: int | None = None
    ema: Decimal | None = None
    breaker: BreakerState = field(default_factory=BreakerState)
    raw_history: tuple[tuple[int, int], ...] = ()  # (wall ts, raw price)
    price_history: tuple[int, ...] = ()  # applied prices, newest last
    prev_terms: dict[str, Any] | None = None  # for the driver attribution


@dataclass(frozen=True)
class Tick:
    ts: int  # wall/chain clock: the time the quote is stamped with
    market_time: int  # market clock: which weather hour / local hour the model used
    price: int  # applied price, mUSDC base units per kWh
    raw_price: int  # clamped raw price (before EMA and limiter)
    ema_price: Decimal
    supply_w: int
    demand_w: int
    band_bps: int
    breaker: BreakerState
    weather_stale: bool
    halted: bool  # quote withheld because halt_quotes_on_freeze is set and the breaker is FROZEN
    vol_bps: Decimal | None
    drivers: dict[str, Any]
    events: tuple[dict[str, Any], ...]


# ------------------------------------------------------------------------------ helpers
def _stale(inputs: MarketInputs, p: PricingParams) -> tuple[bool, str]:
    bad = [
        f"{c.cell_id} ({'no data' if c.weather_source == 'none' else f'{c.weather_age_s}s old'})"
        for c in inputs.cells
        if c.capacity_w > 0
        and (c.weather_source == "none" or c.weather_age_s > p.weather_stale_after_s)
    ]
    if not bad:
        return False, ""
    return True, f"weather stale (> {p.weather_stale_after_s}s) for cell(s): " + ", ".join(bad)


def _cell_driver(c: CellInput, p: PricingParams) -> dict[str, Any]:
    t_cell = cell_temperature(c.temp_c, c.ghi_wm2, p.cell_temp_rise)
    return {
        "cell": c.cell_id,
        "meters": c.meters,
        "capacity_w": c.capacity_w,
        "ghi_wm2": ds(c.ghi_wm2, 3),
        "temp_c": ds(c.temp_c, 2),
        "cloud_cover_pct": None if c.cloud_cover_pct is None else ds(c.cloud_cover_pct, 1),
        "cell_temp_c": ds(t_cell, 2),
        "derate": ds(temp_derate(t_cell, p.temp_coeff), 6),
        "generation_kw": ds(generation_kw(c.capacity_w, c.ghi_wm2, c.temp_c, p)),
        "age_s": c.weather_age_s,
        "source": c.weather_source,
    }


def _attribution(
    prev: dict[str, Any] | None, now: dict[str, Any], eps: Decimal, raw_unclamped_changed: bool
) -> dict[str, Any] | None:
    """Split the change in ln(raw price) between demand and supply (generation vs inventory)."""
    if prev is None:
        return None
    d_now, d_prev = D(now["demand_kw"]), D(prev["demand_kw"])
    s_now, s_prev = D(now["supply_eff_kw"]), D(prev["supply_eff_kw"])
    if min(d_now, d_prev, s_now, s_prev) <= 0:
        return None
    dln_d = CTX.ln(d_now / d_prev)
    dln_s = CTX.ln(s_now / s_prev)
    d_gen = D(now["weighted_generation_kw"]) - D(prev["weighted_generation_kw"])
    d_inv = D(now["weighted_inventory_kw"]) - D(prev["weighted_inventory_kw"])
    d_sup = d_gen + d_inv
    gen_share = d_gen / d_sup if d_sup != 0 else D(0)
    supply_bps = -eps * dln_s * D(10_000)
    return {
        "from_demand_bps": ds(eps * dln_d * D(10_000), 2),
        "from_supply_bps": ds(supply_bps, 2),
        "of_which_generation_bps": ds(supply_bps * gen_share, 2),
        "of_which_inventory_bps": ds(supply_bps * (D(1) - gen_share), 2),
        "approximate": bool(raw_unclamped_changed),
        "note": "d ln(P_raw) = eps * (d ln D - d ln S_eff); approximate when a cap or clamp is active",
    }


# ---------------------------------------------------------------------------------- step
def step(
    state: EngineState, inputs: MarketInputs, params: PricingParams, now: int
) -> tuple[EngineState, Tick]:
    """Advance the engine by one tick at wall/chain time `now`."""
    with localcontext(CTX):
        return _step(state, inputs, params, now)


def _step(
    state: EngineState, inputs: MarketInputs, p: PricingParams, now: int
) -> tuple[EngineState, Tick]:
    s = supply_terms(inputs, p)
    d = demand_terms(inputs, p)
    raw = raw_price(s, d, p)

    # --- circuit breaker: evaluated on the RAW price, before the EMA and the limiter -------
    stale, stale_detail = _stale(inputs, p)
    history = tuple((t, r) for t, r in state.raw_history if now - t <= p.move_window_s)
    breaker, events, move_bps = brk.evaluate(
        state.breaker,
        now=now,
        weather_stale=stale,
        stale_detail=stale_detail,
        raw_history=history,
        raw_now=raw.price,
        params=p,
    )
    # Operator reset requests are applied by the service (brk.reset) before calling step().

    # --- smoothing --------------------------------------------------------------------
    clamps: list[str] = []
    if s.floor_applied:
        clamps.append("s_min")
    if raw.ratio_capped:
        clamps.append("ratio_cap")
    if raw.clamp:
        clamps.append(f"raw_{raw.clamp}")

    previous = state.last_price
    new_ema = state.ema
    limiter: dict[str, Any] = {"applied": False, "direction": None, "max_step_bps": p.max_step_bps}
    held = breaker.holds_price
    if held:
        applied = previous if previous is not None else p.p_base
        clamps.append("held")
        target = applied
    else:
        new_ema = ema(state.ema, raw.price, p.ema_alpha)
        target = clamp(quantize_price(new_ema), p.p_floor, p.p_ceiling)
        if previous is None:
            applied = target
        else:
            lim = limit_step(previous, target, p.max_step_bps)
            applied = lim.price
            limiter.update(max_up=lim.max_up, max_down=lim.max_down)
            if lim.direction:
                limiter.update(applied=True, direction=lim.direction)
                clamps.append(f"limiter_{lim.direction}")
        final = clamp(applied, p.p_floor, p.p_ceiling)
        if final != applied:
            clamps.append("final_floor" if final > applied else "final_ceiling")
            applied = final

    # --- band hint (never wider than the on-chain band) -------------------------------
    hint = p.band_hint_bps
    if breaker.widens_band:
        hint = int(D(hint) * p.band_widen_multiplier)
    published_band = min(hint, inputs.onchain_band_bps)

    # --- volatility -------------------------------------------------------------------
    prices = (*state.price_history, applied)[-(p.vol_window + 1) :]
    vol = realized_vol_bps(prices, D(3600) / D(p.tick_seconds))

    # --- driver breakdown -------------------------------------------------------------
    terms = {
        "demand_kw": str(d.demand_kw),
        "supply_eff_kw": str(s.effective_kw),
        "weighted_generation_kw": str(s.weighted_generation_kw),
        "weighted_inventory_kw": str(s.weighted_inventory_kw),
        "raw": raw.price,
    }
    attribution = _attribution(
        state.prev_terms, terms, p.epsilon, bool(raw.clamp) or raw.ratio_capped
    )
    if attribution is not None and state.prev_terms is not None:
        attribution["raw_change_bps"] = (
            (raw.price - int(state.prev_terms["raw"])) * 10_000 // int(state.prev_terms["raw"])
            if int(state.prev_terms["raw"]) > 0
            else 0
        )
    drivers: dict[str, Any] = {
        "version": DRIVERS_VERSION,
        "market_time": inputs.market_time,
        "local_solar_hour": ds(d.local_hour, 3),
        "profile_multiplier": ds(d.profile_multiplier, 4),
        "supply": {
            "generation_kw": ds(s.generation_kw),
            "inventory_kw": ds(s.inventory_kw),
            "w_gen": str(p.w_gen),
            "w_inv": str(p.w_inv),
            "weighted_generation_kw": ds(s.weighted_generation_kw),
            "weighted_inventory_kw": ds(s.weighted_inventory_kw),
            "supply_kw": ds(s.supply_kw),
            "s_min_kw": ds(s.s_min_kw),
            "effective_kw": ds(s.effective_kw),
            "floor_applied": s.floor_applied,
            "installed_kw": ds(s.installed_kw),
            "inventory_wh": inputs.inventory_wh,
            "inventory_source": inputs.inventory_source,
        },
        "demand": {
            "load_kw": ds(d.load_kw),
            "base_kw": ds(d.base_kw),
            "bids_kw": ds(d.bids_kw),
            "velocity_kw": ds(d.velocity_kw),
            "demand_kw": ds(d.demand_kw),
            "open_bid_wh": inputs.open_bid_wh,
            "recent_fill_wh": inputs.recent_fill_wh,
        },
        "price": {
            "ratio_uncapped": ds(raw.ratio_uncapped, 6),
            "ratio": ds(raw.ratio, 6),
            "ratio_capped": raw.ratio_capped,
            "neutral": raw.neutral,
            "epsilon": str(p.epsilon),
            "p_base": p.p_base,
            "raw_unclamped": ds(raw.unclamped, 3),
            "raw": raw.price,
        },
        "smoothing": {
            "ema": None if new_ema is None else ds(new_ema, 3),
            "ema_exact": None if new_ema is None else str(new_ema),
            "alpha": str(p.ema_alpha),
            "target": target,
            "previous": previous,
            "applied": applied,
            "held": held,
            "limiter": limiter,
        },
        "clamps": clamps,
        "weather": [_cell_driver(c, p) for c in inputs.cells],
        "breaker": {
            **breaker.to_json(),
            "weather_stale": stale,
            "stale_detail": stale_detail or None,
            "raw_move_bps": move_bps,
            "move_limit_bps": p.move_bps,
            "move_window_s": p.move_window_s,
        },
        "band": {
            "hint_bps": p.band_hint_bps,
            "published_bps": published_band,
            "onchain_band_bps": inputs.onchain_band_bps,
            "widened": breaker.widens_band,
        },
        "attribution": attribution,
        "terms": terms,
    }
    tick = Tick(
        ts=now,
        market_time=inputs.market_time,
        price=applied,
        raw_price=raw.price,
        ema_price=new_ema if new_ema is not None else D(applied),
        supply_w=int((s.supply_kw * D(1000)).to_integral_value()),
        demand_w=int((d.demand_kw * D(1000)).to_integral_value()),
        band_bps=published_band,
        breaker=breaker,
        weather_stale=stale,
        halted=p.halt_quotes_on_freeze and breaker.mode == Mode.FROZEN,
        vol_bps=vol,
        drivers=drivers,
        events=tuple(events),
    )
    keep = max(p.vol_window + 1, 2)
    new_state = EngineState(
        last_price=applied,
        ema=new_ema,
        breaker=breaker,
        raw_history=(*history, (now, raw.price)),
        price_history=prices[-keep:],
        prev_terms=terms,
    )
    return new_state, tick


# ------------------------------------------------------------------------------ forecast
def forecast_row(inputs: MarketInputs, params: PricingParams) -> dict[str, Any]:
    """Supply, demand and the raw price implied by `inputs`, with no smoothing and no state.

    Used for the 24-hour outlook: it shows where the raw price is heading, not where the
    smoothed, rate-limited price will be.
    """
    with localcontext(CTX):
        s = supply_terms(inputs, params)
        d = demand_terms(inputs, params)
        raw = raw_price(s, d, params)
        return {
            "time": inputs.market_time,
            "ghi_wm2": ds(
                sum((c.ghi_wm2 * c.capacity_w for c in inputs.cells), D(0))
                / max(D(1), D(sum(c.capacity_w for c in inputs.cells))),
                1,
            ),
            "generation_kw": ds(s.generation_kw),
            "supply_kw": ds(s.effective_kw),
            "demand_kw": ds(d.demand_kw),
            "raw_price": raw.price,
            "clamps": ([f"raw_{raw.clamp}"] if raw.clamp else [])
            + (["ratio_cap"] if raw.ratio_capped else [])
            + (["s_min"] if s.floor_applied else []),
        }


# --------------------------------------------------------------------------- restart support
def restore_state(rows: Sequence[dict[str, Any]], params: PricingParams) -> EngineState:
    """Rebuild the engine state from persisted ticks (oldest first)."""
    if not rows:
        return EngineState()
    last = rows[-1]
    drivers = last["drivers"]
    ema_exact = drivers["smoothing"].get("ema_exact")
    window = [r for r in rows if last["ts"] - r["ts"] <= params.move_window_s]
    return EngineState(
        last_price=int(last["price"]),
        ema=None if ema_exact is None else D(ema_exact),
        breaker=BreakerState.from_json(
            {
                k: drivers["breaker"][k]
                for k in ("mode", "reason", "since", "healthy_ticks", "quiet_ticks")
            }
        ),
        raw_history=tuple((int(r["ts"]), int(r["raw_price"])) for r in window),
        price_history=tuple(int(r["price"]) for r in rows[-(params.vol_window + 1) :]),
        prev_terms=drivers.get("terms"),
    )


def reset_breaker(state: EngineState, now: int) -> EngineState:
    return replace(state, breaker=brk.reset(state.breaker, now))
