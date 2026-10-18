"""EMA smoothing, the per-tick rate limiter and the realized-volatility estimate. Pure."""

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import ROUND_HALF_EVEN, Decimal
from itertools import pairwise

from app.pricing.model import CTX

D = Decimal


def ema(previous: Decimal | None, raw: int, alpha: Decimal) -> Decimal:
    """EMA_t = alpha * raw_t + (1 - alpha) * EMA_{t-1}; seeded with the first raw price."""
    if previous is None:
        return D(raw)
    return alpha * D(raw) + (D(1) - alpha) * previous


def clamp(value: int, floor: int, ceiling: int) -> int:
    return max(floor, min(ceiling, value))


@dataclass(frozen=True)
class Limited:
    price: int
    direction: str | None  # "up" | "down" when the limiter bound the move, else None
    max_up: int
    max_down: int


def limit_step(previous: int, target: int, max_step_bps: int) -> Limited:
    """Move from `previous` toward `target` by at most `max_step_bps` basis points.

    The permitted delta is floor(previous * bps / 10000): integer rounding is always toward
    `previous`, so the change can never exceed the limit, not even by one unit.
    """
    delta = previous * max_step_bps // 10_000
    max_up, max_down = previous + delta, previous - delta
    if target > max_up:
        return Limited(max_up, "up", max_up, max_down)
    if target < max_down:
        return Limited(max_down, "down", max_up, max_down)
    return Limited(target, None, max_up, max_down)


def quantize_price(value: Decimal) -> int:
    return int(value.quantize(D(1), rounding=ROUND_HALF_EVEN))


def realized_vol_bps(prices: Sequence[int], ticks_per_hour: Decimal) -> Decimal | None:
    """Realized volatility of the applied price in bps per sqrt(hour), or None if < 2 returns.

    sigma_tick = sqrt(mean(r_i ** 2)) with r_i = ln(p_i / p_{i-1}); scaled by sqrt(ticks/hour).
    """
    returns = [CTX.ln(D(b) / D(a)) for a, b in pairwise(prices) if a > 0 and b > 0]
    if len(returns) < 2:
        return None
    mean_sq = sum((r * r for r in returns), D(0)) / D(len(returns))
    return CTX.sqrt(mean_sq) * CTX.sqrt(ticks_per_hour) * D(10_000)
