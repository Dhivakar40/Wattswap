"""Pure matching, price-band and funding maths (no services)."""

from hypothesis import given, settings
from hypothesis import strategies as st

from app.matching.book import BookOrder, Match, MatchContext, match_book
from app.matching.funding import Commitment, Funds, orders_to_cancel, total_commitment
from app.matching.rules import (
    band_bounds,
    cost_of,
    feasible_price,
    fee_of,
    fill_is_worth_settling,
    max_buy_cost,
)

QUOTE = 150_000
BAND = 500  # 5 % on-chain


def ctx(**kw: object) -> MatchContext:
    base: dict[str, object] = {
        "reference_price": QUOTE,
        "quote_price": QUOTE,
        "onchain_band_bps": BAND,
        "band_safety_pct": 90,
        "fee_bps": 100,
        "min_fill_cost": 1000,
    }
    base.update(kw)
    return MatchContext(**base)  # type: ignore[arg-type]


def o(key: str, maker: str, price: int, seq: int, wh: int) -> BookOrder:
    return BookOrder(key, "0x" + key.encode().hex().ljust(64, "0"), maker, price, seq, wh)


# ------------------------------------------------------------------------- contract maths
def test_cost_and_fee_use_the_contracts_floor_division() -> None:
    assert cost_of(1000, 150_000) == 150_000  # 1 kWh
    assert cost_of(333, 150_000) == 49_950
    assert cost_of(1, 999) == 0  # would be DustFill on-chain
    assert fee_of(49_950, 100) == 499
    assert max_buy_cost(2500, 160_000) == 400_000


def test_band_bounds_hand_computed() -> None:
    # usable band = 500 * 90 / 100 = 450 bps -> width = 150_000 * 450 / 10_000 = 6_750
    assert band_bounds(150_000, 500, 90) == (143_250, 156_750)
    # the full on-chain band would be 7_500; the margin keeps us strictly inside it
    assert band_bounds(150_000, 500, 100) == (142_500, 157_500)


def test_feasible_price_clamps_reference_into_limits_and_band() -> None:
    assert feasible_price(150_000, 140_000, 160_000, QUOTE, BAND, 90) == 150_000
    assert feasible_price(150_000, 152_000, 160_000, QUOTE, BAND, 90) == 152_000  # clamp to ask
    assert feasible_price(150_000, 140_000, 148_000, QUOTE, BAND, 90) == 148_000  # clamp to bid
    # limits cross, but both sit above the band: exec would revert with PriceOutsideBand
    assert feasible_price(150_000, 200_000, 210_000, QUOTE, BAND, 90) is None
    assert feasible_price(150_000, 100_000, 120_000, QUOTE, BAND, 90) is None


def test_dust_rule_matches_the_contract() -> None:
    assert not fill_is_worth_settling(1, 999, 1)  # cost 0
    assert not fill_is_worth_settling(100, 5_000, 1000)  # cost 500 < min_fill_cost
    assert fill_is_worth_settling(100, 20_000, 1000)  # cost 2000


# ------------------------------------------------------------------------------ matching
def test_price_time_priority_and_partial_fills() -> None:
    bids = [o("b1", "0xb1", 160_000, 2, 1000), o("b2", "0xb2", 165_000, 3, 1000)]
    asks = [o("a1", "0xa1", 140_000, 1, 1500)]
    matches, _ = match_book(bids, asks, ctx())
    assert [(m.bid.key, m.fill_wh) for m in matches] == [
        ("b2", 1000),
        ("b1", 500),
    ]  # best bid first


def test_equal_price_is_resolved_by_time() -> None:
    bids = [o("late", "0xb2", 160_000, 9, 500), o("early", "0xb1", 160_000, 4, 500)]
    asks = [o("a1", "0xa1", 150_000, 1, 500)]
    matches, _ = match_book(bids, asks, ctx())
    assert [m.bid.key for m in matches] == ["early"]


def test_self_trade_is_skipped_not_matched_and_next_best_is_used() -> None:
    bids = [o("b1", "0xaa", 160_000, 5, 1000)]
    asks = [o("own", "0xaa", 140_000, 1, 1000), o("other", "0xcc", 145_000, 2, 1000)]
    matches, skipped = match_book(bids, asks, ctx())
    assert [m.ask.key for m in matches] == ["other"]
    assert skipped["self_trade"] == 1


def test_only_own_orders_means_no_match() -> None:
    matches, skipped = match_book(
        [o("b", "0xaa", 160_000, 2, 1000)], [o("a", "0xaa", 140_000, 1, 1000)], ctx()
    )
    assert matches == [] and skipped["self_trade"] == 1


def test_band_infeasible_pair_stays_on_the_book() -> None:
    matches, skipped = match_book(
        [o("b", "0xb", 210_000, 2, 1000)], [o("a", "0xa", 200_000, 1, 1000)], ctx()
    )
    assert matches == [] and skipped["band_infeasible"] == 1


def test_cooled_pair_is_skipped() -> None:
    b, a = o("b", "0xb", 160_000, 2, 1000), o("a", "0xa", 140_000, 1, 1000)
    matches, skipped = match_book([b], [a], ctx(cooled_pairs={(b.order_hash, a.order_hash)}))
    assert matches == [] and skipped["cooldown"] == 1


def test_unfunded_makers_are_blocked() -> None:
    matches, skipped = match_book(
        [o("b", "0xb", 160_000, 2, 1000)],
        [o("a", "0xa", 140_000, 1, 1000)],
        ctx(blocked_makers={"0xa"}),
    )
    assert matches == [] and skipped["unfunded"] == 1


def test_non_crossing_book_has_no_matches() -> None:
    matches, _ = match_book(
        [o("b", "0xb", 140_000, 2, 1000)], [o("a", "0xa", 150_000, 1, 1000)], ctx()
    )
    assert matches == []


def test_dust_cost_fill_is_not_produced() -> None:
    matches, skipped = match_book(
        [o("b", "0xb", 20_000, 2, 10)],
        [o("a", "0xa", 10_000, 1, 10)],
        ctx(reference_price=15_000, quote_price=15_000),
    )
    assert matches == [] and skipped["dust_cost"] == 1


order_st = st.tuples(
    st.sampled_from(["0xm1", "0xm2", "0xm3", "0xm4"]),
    st.integers(100_000, 200_000),
    st.integers(1, 5000),
)


def _book(rows: list[tuple[str, int, int]], side: str) -> list[BookOrder]:
    return [o(f"{side}{i}", m, p, i, wh) for i, (m, p, wh) in enumerate(rows)]


@settings(max_examples=300, deadline=None)
@given(
    bids=st.lists(order_st, max_size=8),
    asks=st.lists(order_st, max_size=8),
    ref=st.integers(120_000, 180_000),
)
def test_matching_properties(
    bids: list[tuple[str, int, int]], asks: list[tuple[str, int, int]], ref: int
) -> None:
    b_book, a_book = _book(bids, "b"), _book(asks, "a")
    c = ctx(reference_price=ref, quote_price=ref)
    matches, _ = match_book(b_book, a_book, c)
    again, _ = match_book(list(reversed(b_book)), list(reversed(a_book)), c)
    assert [(m.bid.key, m.ask.key, m.fill_wh, m.exec_price) for m in matches] == [
        (m.bid.key, m.ask.key, m.fill_wh, m.exec_price) for m in again
    ]  # deterministic, input order irrelevant

    used_bid: dict[str, int] = {}
    used_ask: dict[str, int] = {}
    lo, hi = band_bounds(ref, BAND, 90)
    for m in matches:
        assert m.bid.maker != m.ask.maker  # never a self trade
        assert m.ask.limit_price <= m.exec_price <= m.bid.limit_price  # limits respected
        assert lo <= m.exec_price <= hi  # inside the quote band
        assert m.cost > 0 and m.cost == cost_of(m.fill_wh, m.exec_price)  # never a DustFill
        used_bid[m.bid.key] = used_bid.get(m.bid.key, 0) + m.fill_wh
        used_ask[m.ask.key] = used_ask.get(m.ask.key, 0) + m.fill_wh
    for b in b_book:
        assert used_bid.get(b.key, 0) <= b.matchable_wh  # conservation: never overfill
    for a in a_book:
        assert used_ask.get(a.key, 0) <= a.matchable_wh
    assert sum(used_bid.values()) == sum(used_ask.values())


def test_matches_are_a_plain_dataclass() -> None:
    m = Match(o("b", "0xb", 1, 1, 1), o("a", "0xa", 1, 1, 1), 1, 1, 0, 0)
    assert m.fill_wh == 1


# ------------------------------------------------------------------------------- funding
def commit(key: str, sell: bool, wh: int, price: int, seq: int) -> Commitment:
    return Commitment(key, "0x" + key, sell, wh, price, seq)


def test_sell_commitment_is_wh_and_buy_commitment_is_worst_case_cost() -> None:
    assert total_commitment([commit("a", True, 1500, 0, 1), commit("b", True, 500, 0, 2)]) == 2000
    assert total_commitment([commit("c", False, 2500, 160_000, 1)]) == 400_000


def test_newest_orders_are_cancelled_first_until_the_rest_fits() -> None:
    orders = [
        commit("o1", True, 1000, 0, 1),
        commit("o2", True, 1000, 0, 2),
        commit("o3", True, 1000, 0, 3),
    ]
    out = orders_to_cancel(orders, Funds(balance=2500, allowance=10_000))
    assert [c.key for c, _ in out] == ["o3"] and out[0][1] == "balance"
    out = orders_to_cancel(orders, Funds(balance=10_000, allowance=1500))
    assert [c.key for c, _ in out] == ["o3", "o2"] and {r for _, r in out} == {"allowance"}
    assert orders_to_cancel(orders, Funds(3000, 3000)) == []


def test_everything_is_cancelled_when_nothing_is_funded() -> None:
    orders = [commit("o1", False, 1000, 100_000, 1), commit("o2", False, 1000, 100_000, 2)]
    assert [c.key for c, _ in orders_to_cancel(orders, Funds(0, 0))] == ["o2", "o1"]


@given(
    rows=st.lists(st.tuples(st.integers(1, 5000), st.integers(1000, 200_000)), max_size=12),
    balance=st.integers(0, 2_000_000),
    allowance=st.integers(0, 2_000_000),
)
def test_remaining_orders_always_fit_and_oldest_survive(
    rows: list[tuple[int, int]], balance: int, allowance: int
) -> None:
    orders = [commit(f"o{i}", False, wh, p, i) for i, (wh, p) in enumerate(rows)]
    funds = Funds(balance, allowance)
    removed = {c.key for c, _ in orders_to_cancel(orders, funds)}
    kept = [c for c in orders if c.key not in removed]
    assert funds.binding_reason(total_commitment(kept)) is None
    # removal is a suffix by age: no kept order is newer than a removed one
    if removed and kept:
        assert max(c.seq for c in kept) < min(c.seq for c in orders if c.key in removed)
