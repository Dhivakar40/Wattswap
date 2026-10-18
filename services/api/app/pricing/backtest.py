"""Backtest / replay: run the pricing engine over historical Open-Meteo data.

    uv run python -m app.pricing.backtest --lat 13.0827 --lon 80.2707 \\
        --from 2026-06-01 --to 2026-06-07 --capacity-kw 5 --out series.csv

The replay uses the SAME pure `engine.step` as production, at the real tick cadence (so the
per-tick limiter behaves exactly as it does live), and prints a summary: price statistics,
realized volatility, how often the limiter and the clamps bound, and breaker activity.
It is deterministic: identical inputs give a byte-identical series.

Simplifications, deliberately visible: the market holds a single cell, bids and fills are
zero, and tradeable inventory follows a simple ledger (surplus generation accumulates up to a
cap, unmet load drains it) because there is no order book to read in a replay.
"""

import argparse
import asyncio
import csv
import json
import sys
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from itertools import pairwise
from pathlib import Path
from typing import Any

import httpx

from app.pricing.engine import EngineState, Tick, step
from app.pricing.model import CellInput, MarketInputs, demand_terms, supply_terms
from app.pricing.params import ParamError, PricingParams, from_mapping
from app.pricing.region import Cell, cell_of
from app.pricing.smoothing import realized_vol_bps
from app.pricing.weather import OpenMeteo, Series, WeatherError, interpolate_series, parse_location

D = Decimal
FIELDS = (
    "ts_iso", "market_time", "ghi_wm2", "supply_kw", "demand_kw", "inventory_wh",
    "raw_price", "ema_price", "price", "limiter", "breaker", "clamps",
)  # fmt: skip


class BacktestError(Exception):
    """Unusable input (exit code 2)."""


@dataclass
class Summary:
    ticks: int = 0
    start: str = ""
    end: str = ""
    price_min: int = 0
    price_max: int = 0
    price_mean: str = ""
    price_p05: int = 0
    price_p95: int = 0
    raw_min: int = 0
    raw_max: int = 0
    realized_vol_bps: str | None = None
    max_step_bps: int = 0
    limiter_binding_pct: str = ""
    clamp_counts: dict[str, int] = field(default_factory=dict)
    breaker_ticks: dict[str, int] = field(default_factory=dict)
    anomaly_episodes: int = 0
    freezes: int = 0
    weather_hours_missing: int = 0

    def to_json(self) -> dict[str, Any]:
        return dict(self.__dict__)


# ------------------------------------------------------------------------------ weather
async def load_weather(
    cell: Cell,
    start: date,
    end: date,
    *,
    fixture: Path | None,
    cache_dir: Path | None,
    client: httpx.AsyncClient | None = None,
    provider: OpenMeteo | None = None,
) -> Series:
    """Hourly series for [start - 1 day, end + 1 day] so interpolation never runs off the edge."""
    if fixture is not None:
        return _read_fixture(fixture)
    own_client = None
    if provider is None:
        own_client = client or httpx.AsyncClient()
        provider = OpenMeteo(own_client, retries=3, retry_delay_s=2.0)
    series: Series = {}
    try:
        day = start - timedelta(days=1)
        while day <= end + timedelta(days=1):
            cached = (
                None if cache_dir is None else cache_dir / f"{cell.id.replace(':', '_')}_{day}.json"
            )
            hit = _read_cache(cached)
            if hit is not None:
                part = hit
            else:
                try:
                    part = await provider.fetch_day(cell, day)
                except WeatherError as exc:
                    raise BacktestError(f"Open-Meteo unavailable for {day}: {exc}") from exc
                _write_cache(cached, part)
            series.update(part)
            day += timedelta(days=1)
    finally:
        if own_client is not None:
            await own_client.aclose()
    return series


def _read_fixture(path: Path) -> Series:
    try:
        return parse_location(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, ValueError, WeatherError) as exc:
        raise BacktestError(f"cannot read fixture {path}: {exc}") from exc


def _read_cache(path: Path | None) -> Series | None:
    if path is None or not path.exists():
        return None
    return parse_location(json.loads(path.read_text(encoding="utf-8")))


def _write_cache(path: Path | None, part: Series) -> None:
    if path is not None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(_cache_to_payload(part)), encoding="utf-8")


def _cache_to_payload(part: Series) -> dict[str, Any]:
    return {
        "hourly": {
            "time": list(part),
            "shortwave_radiation": [float(p.ghi_wm2) for p in part.values()],
            "temperature_2m": [float(p.temp_c) for p in part.values()],
        }
    }


# --------------------------------------------------------------------------------- replay
@dataclass(frozen=True)
class Scenario:
    cell: Cell
    capacity_w: int
    start_ts: int
    end_ts: int
    params: PricingParams
    sample_s: int = 900
    inventory_model: str = "ledger"  # ledger | fixed:<Wh> | none
    inventory_cap_wh: int | None = None


def _inventory_start(model: str) -> int:
    if model.startswith("fixed:"):
        return int(model.split(":", 1)[1])
    return 0


def replay(scn: Scenario, series: Series) -> tuple[list[dict[str, Any]], list[Tick], int]:
    """Run the engine over the scenario. Returns (sampled rows, every tick, missing-weather ticks)."""
    p = scn.params
    cap_wh = scn.inventory_cap_wh if scn.inventory_cap_wh is not None else scn.capacity_w * 6
    inventory = _inventory_start(scn.inventory_model)
    state = EngineState()
    rows: list[dict[str, Any]] = []
    ticks: list[Tick] = []
    missing = 0
    dt_h = D(p.tick_seconds) / D(3600)
    for now in range(scn.start_ts, scn.end_ts, p.tick_seconds):
        point = interpolate_series(series, now)
        if point is None:
            missing += 1
            continue
        cell = CellInput(
            cell_id=scn.cell.id,
            capacity_w=scn.capacity_w,
            meters=1,
            center_lon_e6=scn.cell.center_lon_e6,
            ghi_wm2=point.ghi_wm2,
            temp_c=point.temp_c,
            cloud_cover_pct=point.cloud_cover_pct,
            weather_age_s=0,
            weather_source="archive",
        )
        inputs = MarketInputs(
            market_time=now,
            cells=(cell,),
            inventory_wh=inventory,
            inventory_source="backtest_" + scn.inventory_model.split(":")[0],
        )
        state, tick = step(state, inputs, p, now)
        ticks.append(tick)
        if scn.inventory_model == "ledger":
            sup, dem = supply_terms(inputs, p), demand_terms(inputs, p)
            net_kw = sup.generation_kw - dem.load_kw
            inventory = max(
                0, min(cap_wh, inventory + int((net_kw * D(1000) * dt_h).to_integral_value()))
            )
        if (now - scn.start_ts) % scn.sample_s == 0:
            d = tick.drivers
            rows.append(
                {
                    "ts_iso": datetime.fromtimestamp(now, UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "market_time": now,
                    "ghi_wm2": d["weather"][0]["ghi_wm2"],
                    "supply_kw": d["supply"]["effective_kw"],
                    "demand_kw": d["demand"]["demand_kw"],
                    "inventory_wh": inventory,
                    "raw_price": tick.raw_price,
                    "ema_price": d["smoothing"]["ema"],
                    "price": tick.price,
                    "limiter": d["smoothing"]["limiter"]["direction"] or "",
                    "breaker": tick.breaker.mode.value,
                    "clamps": "|".join(d["clamps"]),
                }
            )
    return rows, ticks, missing


def summarize(ticks: Sequence[Tick], missing: int, p: PricingParams) -> Summary:
    if not ticks:
        raise BacktestError("no ticks were produced (no weather for the requested period)")
    prices = sorted(t.price for t in ticks)
    n = len(prices)
    clamps: dict[str, int] = {}
    breaker: dict[str, int] = {}
    binding = 0
    for t in ticks:
        for c in t.drivers["clamps"]:
            clamps[c] = clamps.get(c, 0) + 1
        breaker[t.breaker.mode.value] = breaker.get(t.breaker.mode.value, 0) + 1
        binding += t.drivers["smoothing"]["limiter"]["applied"]
    steps = [abs(b.price - a.price) * 10_000 // a.price for a, b in pairwise(ticks)]
    vol = realized_vol_bps([t.price for t in ticks], D(3600) / D(p.tick_seconds))
    kinds = [e["kind"] for t in ticks for e in t.events]
    mean = D(sum(prices)) / D(n)
    return Summary(
        ticks=n,
        start=datetime.fromtimestamp(ticks[0].ts, UTC).isoformat(),
        end=datetime.fromtimestamp(ticks[-1].ts, UTC).isoformat(),
        price_min=prices[0],
        price_max=prices[-1],
        price_mean=str(mean.quantize(D("0.01"))),
        price_p05=prices[int(n * 0.05)],
        price_p95=prices[min(n - 1, int(n * 0.95))],
        raw_min=min(t.raw_price for t in ticks),
        raw_max=max(t.raw_price for t in ticks),
        realized_vol_bps=None if vol is None else f"{vol:.2f}",
        max_step_bps=max(steps, default=0),
        limiter_binding_pct=f"{D(binding) * 100 / D(n):.2f}",
        clamp_counts=dict(sorted(clamps.items())),
        breaker_ticks=dict(sorted(breaker.items())),
        anomaly_episodes=kinds.count("anomaly_start"),
        freezes=kinds.count("breaker_frozen"),
        weather_hours_missing=missing * p.tick_seconds // 3600,
    )


def format_summary(s: Summary, scn: Scenario) -> str:
    def usdc(x: int) -> str:
        return f"{D(x) / D(1_000_000):.6f}"

    lines = [
        f"Backtest {s.start} .. {s.end}  ({s.ticks} ticks, cell {scn.cell.id}, {scn.capacity_w / 1000:g} kW)",
        f"  price (applied)  min {usdc(s.price_min)}  p05 {usdc(s.price_p05)}  mean {D(s.price_mean) / D(1_000_000):.6f}"
        f"  p95 {usdc(s.price_p95)}  max {usdc(s.price_max)}  mUSDC/kWh",
        f"  raw price        min {usdc(s.raw_min)}  max {usdc(s.raw_max)}",
        f"  realized vol     {s.realized_vol_bps} bps per sqrt(hour)",
        f"  largest tick step {s.max_step_bps} bps (limit {scn.params.max_step_bps})",
        f"  limiter binding  {s.limiter_binding_pct}% of ticks",
        f"  clamps           {s.clamp_counts or 'none'}",
        f"  breaker          {s.breaker_ticks}  anomalies={s.anomaly_episodes}  freezes={s.freezes}",
    ]
    return "\n".join(lines)


# ------------------------------------------------------------------------------------ CLI
def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="python -m app.pricing.backtest",
        description="Replay the WattSwap pricing engine over historical Open-Meteo data.",
    )
    ap.add_argument("--lat", type=Decimal, required=True)
    ap.add_argument("--lon", type=Decimal, required=True)
    ap.add_argument("--from", dest="start", type=date.fromisoformat, required=True)
    ap.add_argument("--to", dest="end", type=date.fromisoformat, required=True, help="inclusive")
    ap.add_argument("--capacity-kw", type=Decimal, default=D(5))
    ap.add_argument("--params-file", type=Path, help="JSON object of parameter overrides")
    ap.add_argument(
        "--tick-seconds",
        type=int,
        default=15,
        help="engine tick (default 15 = live cadence; coarser is faster but the per-tick limiter then allows more movement per hour)",
    )
    ap.add_argument("--inventory", default="ledger", help="ledger | none | fixed:<Wh>")
    ap.add_argument("--inventory-cap-kwh", type=Decimal)
    ap.add_argument("--sample-seconds", type=int, default=900, help="spacing of rows in the output")
    ap.add_argument("--out", type=Path, help="write the sampled series as CSV")
    ap.add_argument("--json", action="store_true", help="print the summary as JSON")
    ap.add_argument(
        "--fixture", type=Path, help="read one Open-Meteo location JSON instead of the network"
    )
    ap.add_argument("--cache-dir", type=Path, help="cache downloaded days here")
    return ap


def run(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.end < args.start:
            raise BacktestError("--to must not be before --from")
        if args.inventory not in ("ledger", "none") and not args.inventory.startswith("fixed:"):
            raise BacktestError("--inventory must be ledger, none or fixed:<Wh>")
        params = PricingParams()
        if args.params_file:
            try:
                params = from_mapping(json.loads(args.params_file.read_text(encoding="utf-8")))
            except (OSError, ValueError) as exc:
                raise BacktestError(f"bad --params-file: {exc}") from exc
        params = from_mapping({}, base=params).with_overrides({"tick_seconds": args.tick_seconds})
        cell = cell_of(int(args.lat * 1_000_000), int(args.lon * 1_000_000))
        series = asyncio.run(
            load_weather(cell, args.start, args.end, fixture=args.fixture, cache_dir=args.cache_dir)
        )
        start_ts = int(
            datetime(args.start.year, args.start.month, args.start.day, tzinfo=UTC).timestamp()
        )
        end_ts = (
            int(datetime(args.end.year, args.end.month, args.end.day, tzinfo=UTC).timestamp())
            + 86_400
        )
        scn = Scenario(
            cell=cell,
            capacity_w=int(args.capacity_kw * 1000),
            start_ts=start_ts,
            end_ts=end_ts,
            params=params,
            sample_s=args.sample_seconds,
            inventory_model=args.inventory,
            inventory_cap_wh=None
            if args.inventory_cap_kwh is None
            else int(args.inventory_cap_kwh * 1000),
        )
        rows, ticks, missing = replay(scn, series)
        summary = summarize(ticks, missing, params)
    except (BacktestError, ParamError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        with args.out.open("w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=FIELDS, lineterminator="\n")
            w.writeheader()
            w.writerows(rows)
    print(json.dumps(summary.to_json(), indent=2) if args.json else format_summary(summary, scn))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(run())
