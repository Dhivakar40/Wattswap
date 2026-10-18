"""A reproducible one-day trace of the engine on a hand-specified synthetic day.

Used by the tests and by docs/pricing-model.md so the worked example can be re-run:

    uv run python -m app.pricing.trace

Synthetic day (all inputs are plain numbers, no network):
  * 5 kW installed in one cell at longitude 0 (so local solar time = UTC);
  * clear-sky irradiance from the hourly table below, linearly interpolated;
  * air temperature 22 C + 8 C * (clear-sky GHI / 900);
  * a sudden cloud event 12:30:00-13:15:00 that cuts irradiance to 25 %;
  * tradeable inventory of 24 kWh held all day (a fixed stand-in for a Phase 4 order book);
  * one tick every 15 s from 00:00:00 for 24 hours (5 760 ticks).
"""

from dataclasses import dataclass
from decimal import Decimal

from app.pricing.engine import EngineState, Tick, step
from app.pricing.model import CellInput, MarketInputs
from app.pricing.params import PricingParams

D = Decimal

# GHI (W/m2) at the top of each hour 00..23. Sunrise ~06:00, sunset ~19:00.
CLEAR_SKY_GHI: tuple[int, ...] = (
    0, 0, 0, 0, 0, 0,
    80, 250, 450, 650, 800, 880,
    900, 880, 800, 650, 450, 250,
    80, 0, 0, 0, 0, 0,
)  # fmt: skip

DAY_START = 1_800_000_000 - 1_800_000_000 % 86_400  # a UTC midnight
CLOUD_START = 12 * 3600 + 30 * 60
CLOUD_END = 13 * 3600 + 15 * 60
CLOUD_FACTOR = D("0.25")
CAPACITY_W = 5000
INVENTORY_WH = 24_000


def clear_ghi(sec_of_day: int) -> Decimal:
    hour, rem = divmod(sec_of_day, 3600)
    a, b = CLEAR_SKY_GHI[hour % 24], CLEAR_SKY_GHI[(hour + 1) % 24]
    return D(a) + (D(b) - D(a)) * D(rem) / D(3600)


def weather_at(sec_of_day: int, *, cloud_event: bool = True) -> tuple[Decimal, Decimal]:
    """(GHI, air temperature) at a second of the day."""
    clear = clear_ghi(sec_of_day)
    temp = D(22) + D(8) * clear / D(900)
    ghi = clear * CLOUD_FACTOR if cloud_event and CLOUD_START <= sec_of_day < CLOUD_END else clear
    return ghi, temp


def inputs_at(sec_of_day: int, *, age_s: int = 0, cloud_event: bool = True) -> MarketInputs:
    ghi, temp = weather_at(sec_of_day, cloud_event=cloud_event)
    cell = CellInput(
        cell_id="trace",
        capacity_w=CAPACITY_W,
        meters=1,
        center_lon_e6=0,
        ghi_wm2=ghi,
        temp_c=temp,
        weather_age_s=age_s,
        weather_source="live",
    )
    return MarketInputs(
        market_time=DAY_START + sec_of_day,
        cells=(cell,),
        inventory_wh=INVENTORY_WH,
        inventory_source="held_balances",
        onchain_band_bps=500,
    )


def run_day(
    params: PricingParams | None = None,
    *,
    cloud_event: bool = True,
    stale_from: int | None = None,
    stale_until: int | None = None,
) -> list[Tick]:
    """Run the engine over the synthetic day. `stale_from/until` (seconds of day) simulate a
    weather outage: the weather ages in wall-clock time and is reported with its real age."""
    p = params or PricingParams()
    state = EngineState()
    ticks: list[Tick] = []
    stale_since: int | None = None
    for sec in range(0, 86_400, p.tick_seconds):
        age = 0
        if stale_from is not None and stale_until is not None and stale_from <= sec < stale_until:
            stale_since = stale_from if stale_since is None else stale_since
            age = sec - stale_since
        else:
            stale_since = None
        state, tick = step(
            state, inputs_at(sec, age_s=age, cloud_event=cloud_event), p, DAY_START + sec
        )
        ticks.append(tick)
    return ticks


@dataclass(frozen=True)
class Checkpoint:
    label: str
    sec_of_day: int


CHECKPOINTS: tuple[Checkpoint, ...] = (
    Checkpoint("midnight", 0),
    Checkpoint("sunrise", 6 * 3600 + 1800),
    Checkpoint("noon", 12 * 3600),
    Checkpoint("cloud onset (+15 s)", CLOUD_START + 15),
    Checkpoint("cloud (+10 min)", CLOUD_START + 600),
    Checkpoint("cloud ends (+0 s)", CLOUD_END),
    Checkpoint("anomaly cleared", 14 * 3600),
    Checkpoint("sunset", 18 * 3600 + 1800),
)


def trace_rows(ticks: list[Tick], p: PricingParams | None = None) -> list[dict[str, str]]:
    p = p or PricingParams()
    rows = []
    for cp in CHECKPOINTS:
        t = ticks[cp.sec_of_day // p.tick_seconds]
        d = t.drivers
        lim = d["smoothing"]["limiter"]
        rows.append(
            {
                "point": cp.label,
                "time": f"{cp.sec_of_day // 3600:02d}:{cp.sec_of_day % 3600 // 60:02d}:{cp.sec_of_day % 60:02d}",
                "ghi": d["weather"][0]["ghi_wm2"],
                "S_eff_kw": d["supply"]["effective_kw"],
                "D_kw": d["demand"]["demand_kw"],
                "raw": str(t.raw_price),
                "ema": d["smoothing"]["ema"] or "-",
                "applied": str(t.price),
                "limiter": lim["direction"] or "-",
                "breaker": t.breaker.mode.value,
                "band_bps": str(t.band_bps),
                "raw_move_bps": str(d["breaker"]["raw_move_bps"]),
            }
        )
    return rows


def format_table(rows: list[dict[str, str]]) -> str:
    cols = list(rows[0])
    widths = {c: max(len(c), *(len(r[c]) for r in rows)) for c in cols}
    line = "| " + " | ".join(c.ljust(widths[c]) for c in cols) + " |"
    sep = "|-" + "-|-".join("-" * widths[c] for c in cols) + "-|"
    body = ["| " + " | ".join(r[c].ljust(widths[c]) for c in cols) + " |" for r in rows]
    return "\n".join([line, sep, *body])


if __name__ == "__main__":  # pragma: no cover
    p = PricingParams()
    print("Synthetic day, cloud event 12:30-13:15\n")
    print(format_table(trace_rows(run_day(p), p)))
