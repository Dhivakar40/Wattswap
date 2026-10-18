"""The circuit breaker: a small, pure state machine.

States       NORMAL  ANOMALY  FROZEN  RECOVERING
Effects      NORMAL     reprice normally
             ANOMALY    reprice normally; the published band hint is widened
             FROZEN     the applied price is HELD (not repriced)
             RECOVERING the applied price is still HELD while health is re-established

Triggers (evaluated every tick)
  stale weather  any cell with installed capacity has weather older than weather_stale_after_s
                 -> FROZEN, from any state.
  raw move       the RAW price (after the floor/ceiling clamp, BEFORE the EMA and the limiter)
                 moved by more than move_bps against any sample within the last move_window_s
                 -> ANOMALY. This never freezes the price; it widens the band hint and emits an
                 anomaly event.

Recovery is hysteretic: FROZEN -> RECOVERING as soon as weather is fresh, then NORMAL only after
recovery_ticks consecutive healthy ticks. An ANOMALY clears after anomaly_hold_ticks ticks
without the raw-move condition. An admin reset forces NORMAL.

docs/pricing-model.md has the transition table and a diagram.
"""

from collections.abc import Sequence
from dataclasses import dataclass, replace
from enum import StrEnum
from typing import Any

from app.pricing.params import PricingParams


class Mode(StrEnum):
    NORMAL = "NORMAL"
    ANOMALY = "ANOMALY"
    FROZEN = "FROZEN"
    RECOVERING = "RECOVERING"


@dataclass(frozen=True)
class BreakerState:
    mode: Mode = Mode.NORMAL
    reason: str | None = None
    since: int = 0  # wall-clock timestamp of the last transition
    healthy_ticks: int = 0  # consecutive healthy ticks while RECOVERING
    quiet_ticks: int = 0  # ticks since the last raw-move trigger while ANOMALY

    @property
    def holds_price(self) -> bool:
        return self.mode in (Mode.FROZEN, Mode.RECOVERING)

    @property
    def widens_band(self) -> bool:
        return self.mode != Mode.NORMAL

    def to_json(self) -> dict[str, Any]:
        return {
            "mode": self.mode.value,
            "reason": self.reason,
            "since": self.since,
            "healthy_ticks": self.healthy_ticks,
            "quiet_ticks": self.quiet_ticks,
        }

    @staticmethod
    def from_json(data: dict[str, Any]) -> "BreakerState":
        return BreakerState(
            Mode(data["mode"]),
            data.get("reason"),
            int(data.get("since", 0)),
            int(data.get("healthy_ticks", 0)),
            int(data.get("quiet_ticks", 0)),
        )


def raw_move_bps(history: Sequence[tuple[int, int]], raw_now: int, now: int, window_s: int) -> int:
    """Largest |raw_now - r| / r (in bps, rounded down) against samples within the window."""
    best = 0
    for ts, r in history:
        if r > 0 and now - ts <= window_s:
            best = max(best, abs(raw_now - r) * 10_000 // r)
    return best


def evaluate(
    state: BreakerState,
    *,
    now: int,
    weather_stale: bool,
    stale_detail: str,
    raw_history: Sequence[tuple[int, int]],
    raw_now: int,
    params: PricingParams,
) -> tuple[BreakerState, list[dict[str, Any]], int]:
    """Advance the breaker by one tick. Returns (new state, events, raw move in bps)."""
    events: list[dict[str, Any]] = []
    move = raw_move_bps(raw_history, raw_now, now, params.move_window_s)
    moved = move > params.move_bps

    def go(mode: Mode, reason: str | None, **kw: int) -> BreakerState:
        return BreakerState(mode, reason, now, kw.get("healthy_ticks", 0), kw.get("quiet_ticks", 0))

    # 1. Stale weather outranks everything.
    if weather_stale:
        if state.mode != Mode.FROZEN:
            events.append(
                {"kind": "breaker_frozen", "reason": stale_detail, "from": state.mode.value}
            )
            return go(Mode.FROZEN, stale_detail), events, move
        return replace(state, reason=stale_detail), events, move

    # 2. Weather is fresh.
    if state.mode == Mode.FROZEN:
        events.append({"kind": "breaker_recovering", "from": "FROZEN"})
        return go(Mode.RECOVERING, "weather fresh again", healthy_ticks=1), events, move
    if state.mode == Mode.RECOVERING:
        healthy = state.healthy_ticks + 1
        if healthy >= params.recovery_ticks:
            events.append({"kind": "breaker_recovered", "from": "RECOVERING"})
            return go(Mode.NORMAL, None), events, move
        return replace(state, healthy_ticks=healthy), events, move

    # 3. Raw-move anomaly detection (never freezes).
    if state.mode == Mode.NORMAL:
        if moved:
            reason = f"raw price moved {move} bps within {params.move_window_s}s (limit {params.move_bps})"
            events.append({"kind": "anomaly_start", "move_bps": move, "reason": reason})
            return go(Mode.ANOMALY, reason), events, move
        return state, events, move
    # ANOMALY
    if moved:
        return replace(state, quiet_ticks=0), events, move
    quiet = state.quiet_ticks + 1
    if quiet >= params.anomaly_hold_ticks:
        events.append({"kind": "anomaly_end"})
        return go(Mode.NORMAL, None), events, move
    return replace(state, quiet_ticks=quiet), events, move


def reset(state: BreakerState, now: int) -> BreakerState:
    """Manual recovery by an administrator."""
    return BreakerState(Mode.NORMAL, None, now, 0, 0)
