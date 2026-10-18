"""Pure price-time-priority matching over an in-memory snapshot of the book.

The matcher worker loads open orders from Postgres, calls `match_book`, and persists the result
inside one transaction. Keeping this pure makes the properties (priority, no self-trade, limits,
band, dust, conservation) testable with hypothesis and independent of any service.
"""

from collections import Counter
from collections.abc import Collection, Iterable
from dataclasses import dataclass, field

from app.matching.rules import cost_of, feasible_price, fee_of, fill_is_worth_settling


@dataclass(frozen=True)
class BookOrder:
    key: str  # row id (opaque)
    order_hash: str
    maker: str  # lowercase
    limit_price: int
    seq: int  # global creation sequence = time priority
    matchable_wh: int  # amount - filled - reserved


@dataclass(frozen=True)
class MatchContext:
    reference_price: int  # latest signed engine price
    quote_price: int
    onchain_band_bps: int
    band_safety_pct: int
    fee_bps: int
    min_fill_cost: int
    cooled_pairs: Collection[tuple[str, str]] = field(default_factory=frozenset)  # (buy, sell)
    blocked_makers: Collection[str] = field(default_factory=frozenset)  # found unfunded
    max_matches: int = 100


@dataclass(frozen=True)
class Match:
    bid: BookOrder
    ask: BookOrder
    fill_wh: int
    exec_price: int
    cost: int
    fee: int


def bid_priority(o: BookOrder) -> tuple[int, int]:
    return (-o.limit_price, o.seq)


def ask_priority(o: BookOrder) -> tuple[int, int]:
    return (o.limit_price, o.seq)


def match_book(
    bids: Iterable[BookOrder], asks: Iterable[BookOrder], ctx: MatchContext
) -> tuple[list[Match], Counter[str]]:
    """Returns (matches, skipped-by-reason counts). Input order is irrelevant; the function sorts."""
    skipped: Counter[str] = Counter()
    ordered_bids = sorted((b for b in bids if b.matchable_wh > 0), key=bid_priority)
    ordered_asks = sorted((a for a in asks if a.matchable_wh > 0), key=ask_priority)
    remaining_ask = {a.key: a.matchable_wh for a in ordered_asks}
    matches: list[Match] = []

    for bid in ordered_bids:
        if bid.maker in ctx.blocked_makers:
            skipped["unfunded"] += 1
            continue
        remaining_bid = bid.matchable_wh
        for ask in ordered_asks:
            if remaining_bid <= 0 or len(matches) >= ctx.max_matches:
                break
            if remaining_ask[ask.key] <= 0:
                continue
            if ask.limit_price > bid.limit_price:
                break  # asks are ascending: nothing further can cross this bid
            if ask.maker == bid.maker:
                skipped["self_trade"] += 1  # never match a maker against themselves
                continue
            if ask.maker in ctx.blocked_makers:
                skipped["unfunded"] += 1
                continue
            if (bid.order_hash, ask.order_hash) in ctx.cooled_pairs:
                skipped["cooldown"] += 1
                continue
            price = feasible_price(
                ctx.reference_price,
                ask.limit_price,
                bid.limit_price,
                ctx.quote_price,
                ctx.onchain_band_bps,
                ctx.band_safety_pct,
            )
            if price is None:
                skipped["band_infeasible"] += 1
                break  # higher asks only make the interval emptier
            fill = min(remaining_bid, remaining_ask[ask.key])
            if not fill_is_worth_settling(fill, price, ctx.min_fill_cost):
                skipped["dust_cost"] += 1
                continue
            cost = cost_of(fill, price)
            matches.append(Match(bid, ask, fill, price, cost, fee_of(cost, ctx.fee_bps)))
            remaining_bid -= fill
            remaining_ask[ask.key] -= fill
        if len(matches) >= ctx.max_matches:
            break
    return matches, skipped
