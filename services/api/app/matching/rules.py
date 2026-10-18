"""Pure trading arithmetic. Every function mirrors the exchange's integer math exactly.

Nothing here does I/O or touches floats: prices are mUSDC base units (6 dp) per kWh, energy is Wh,
and `cost = floor(fill * price / 1000)`, `fee = floor(cost * feeBps / 10000)` as in
`WattSwapExchange._execute`.
"""

WH_PER_KWH = 1_000
BPS = 10_000


def cost_of(fill_wh: int, price: int) -> int:
    """Total the buyer is debited (the seller receives cost - fee, the treasury the fee)."""
    return fill_wh * price // WH_PER_KWH


def fee_of(cost: int, fee_bps: int) -> int:
    return cost * fee_bps // BPS


def max_buy_cost(remaining_wh: int, limit_price: int) -> int:
    """Most a buy order can still cost: the exec price never exceeds the limit."""
    return remaining_wh * limit_price // WH_PER_KWH


def band_bounds(quote_price: int, onchain_band_bps: int, safety_pct: int) -> tuple[int, int]:
    """Inclusive price range the exchange accepts for this quote, shrunk by the safety margin.

    The contract reverts when |exec - quote| * 10000 > quote * bandBps, i.e. it accepts
    |exec - quote| <= floor(quote * bandBps / 10000). We use `safety_pct` of the band so quote drift
    between matching and mining does not push a trade outside it.
    """
    usable_bps = onchain_band_bps * safety_pct // 100
    width = quote_price * usable_bps // BPS
    return max(0, quote_price - width), quote_price + width


def feasible_price(
    reference: int,
    sell_limit: int,
    buy_limit: int,
    quote_price: int,
    onchain_band_bps: int,
    safety_pct: int,
) -> int | None:
    """Execution price for a pair, or None when no price satisfies limits AND the quote band.

    exec = clamp(reference, lo, hi) where [lo, hi] = [sell.limit, buy.limit] ∩ quote band.
    """
    band_lo, band_hi = band_bounds(quote_price, onchain_band_bps, safety_pct)
    lo = max(sell_limit, band_lo)
    hi = min(buy_limit, band_hi)
    if lo > hi:
        return None
    return min(max(reference, lo), hi)


def fill_is_worth_settling(fill_wh: int, price: int, min_fill_cost: int) -> bool:
    """False for fills that would revert with DustFill (cost == 0) or burn gas for nothing."""
    return fill_wh > 0 and cost_of(fill_wh, price) >= max(1, min_fill_cost)
