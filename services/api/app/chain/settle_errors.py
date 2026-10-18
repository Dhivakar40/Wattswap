"""What a `settle()` revert means, and what the relayer must do about it."""

import enum

from app.chain.revert import RevertInfo


class Verdict(enum.StrEnum):
    # An order was voided on-chain between matching and mining. NEVER retried.
    CANCEL_RACE = "cancel_race"
    # An order is unusable (expired, bad signature, would overfill).
    ORDER_BAD = "order_bad"
    # A maker lacks balance/allowance. The relayer re-reads both makers to attribute it.
    UNDERFUNDED = "underfunded"
    # Quote/clock trouble, not the orders' fault: re-quote and retry (bounded).
    TRANSIENT = "transient"
    # The exchange itself refuses (paused, relayer role missing, market disabled).
    SYSTEMIC = "systemic"
    # The matcher produced a pair the contract considers malformed: a bug in our code.
    BUG = "bug"
    # No decodable reason (RPC trouble, unknown selector): retry, bounded.
    UNKNOWN = "unknown"


_TABLE: dict[str, Verdict] = {
    "OrderIsCancelled": Verdict.CANCEL_RACE,
    "NonceTooLow": Verdict.CANCEL_RACE,
    "OrderExpired": Verdict.ORDER_BAD,
    "InvalidOrderSignature": Verdict.ORDER_BAD,
    "Overfill": Verdict.ORDER_BAD,
    "ERC20InsufficientBalance": Verdict.UNDERFUNDED,
    "ERC20InsufficientAllowance": Verdict.UNDERFUNDED,
    "SafeERC20FailedOperation": Verdict.UNDERFUNDED,
    "StaleQuote": Verdict.TRANSIENT,
    "QuoteFromFuture": Verdict.TRANSIENT,
    "PriceOutsideBand": Verdict.TRANSIENT,
    "InvalidQuoteSigner": Verdict.TRANSIENT,
    "EnforcedPause": Verdict.SYSTEMIC,
    "AccessControlUnauthorizedAccount": Verdict.SYSTEMIC,
    "MarketDisabled": Verdict.SYSTEMIC,
    "SelfTrade": Verdict.BUG,
    "PriceOutOfLimits": Verdict.BUG,
    "InvalidSide": Verdict.BUG,
    "MarketMismatch": Verdict.BUG,
    "QuoteMarketMismatch": Verdict.BUG,
    "DustFill": Verdict.BUG,
    "ZeroFill": Verdict.BUG,
}

# Transient errors that also deserve an operator alarm.
ALARMING = frozenset({"InvalidQuoteSigner"})


def classify(info: RevertInfo | None) -> Verdict:
    if info is None:
        return Verdict.UNKNOWN
    return _TABLE.get(info.name, Verdict.UNKNOWN)
