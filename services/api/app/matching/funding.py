"""Over-commitment maths: can a maker's open orders all be honoured at once?

A maker's total commitment per token must fit BOTH their balance and their allowance to the
exchange. These are pure functions; the chain snapshot that feeds them is in `app.matching.snapshot`.
"""

from dataclasses import dataclass

from app.matching.rules import max_buy_cost


@dataclass(frozen=True)
class Commitment:
    """One open order's claim on the maker's funds."""

    key: str
    order_hash: str
    is_sell: bool
    remaining_wh: int  # amount - filled (chain-consistent); Wh reserved in flight still count
    limit_price: int
    seq: int

    @property
    def amount(self) -> int:
        """WATT for a sell; worst-case mUSDC for a buy."""
        if self.is_sell:
            return self.remaining_wh
        return max_buy_cost(self.remaining_wh, self.limit_price)


@dataclass(frozen=True)
class Funds:
    balance: int
    allowance: int

    def binding_reason(self, committed: int) -> str | None:
        """Why `committed` does not fit: 'balance' (checked first) or 'allowance'; None if it fits."""
        if committed > self.balance:
            return "balance"
        if committed > self.allowance:
            return "allowance"
        return None


def total_commitment(orders: list[Commitment]) -> int:
    return sum(o.amount for o in orders)


def orders_to_cancel(orders: list[Commitment], funds: Funds) -> list[tuple[Commitment, str]]:
    """Which orders to drop so the rest fits, NEWEST FIRST so older orders keep time priority.

    Greedy from the newest: remove until the remainder fits. The reason is the constraint that
    was violated when that order was removed.
    """
    kept = sorted(orders, key=lambda o: o.seq)
    removed: list[tuple[Commitment, str]] = []
    committed = total_commitment(kept)
    while kept:
        reason = funds.binding_reason(committed)
        if reason is None:
            break
        newest = kept.pop()
        committed -= newest.amount
        removed.append((newest, reason))
    return removed
