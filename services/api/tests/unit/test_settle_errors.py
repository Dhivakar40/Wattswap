"""What each settle() revert means for the relayer, pinned against the real contract ABIs."""

import json
from pathlib import Path

import pytest

from app.chain.revert import RevertInfo
from app.chain.settle_errors import Verdict, classify

ABIS = Path(__file__).resolve().parents[4] / "packages" / "shared" / "src" / "abis" / "json"

# Errors the exchange itself can raise from settle(); each must be classified on purpose.
SETTLE_ERRORS = {
    "InvalidSide", "MarketMismatch", "MarketDisabled", "SelfTrade", "ZeroFill", "DustFill",
    "OrderExpired", "OrderIsCancelled", "NonceTooLow", "InvalidOrderSignature", "Overfill",
    "PriceOutOfLimits", "QuoteMarketMismatch", "QuoteFromFuture", "StaleQuote",
    "InvalidQuoteSigner", "PriceOutsideBand",
}  # fmt: skip


def test_every_settle_error_of_the_exchange_is_classified() -> None:
    abi = json.loads((ABIS / "WattSwapExchange.json").read_text(encoding="utf-8"))
    declared = {i["name"] for i in abi if i["type"] == "error"}
    assert SETTLE_ERRORS <= declared  # the list above still matches the contract
    for name in SETTLE_ERRORS:
        assert classify(RevertInfo(name)) is not Verdict.UNKNOWN, name


@pytest.mark.parametrize(
    ("name", "verdict"),
    [
        ("OrderIsCancelled", Verdict.CANCEL_RACE),
        ("NonceTooLow", Verdict.CANCEL_RACE),
        ("OrderExpired", Verdict.ORDER_BAD),
        ("InvalidOrderSignature", Verdict.ORDER_BAD),
        ("Overfill", Verdict.ORDER_BAD),
        ("ERC20InsufficientBalance", Verdict.UNDERFUNDED),
        ("ERC20InsufficientAllowance", Verdict.UNDERFUNDED),
        ("StaleQuote", Verdict.TRANSIENT),
        ("QuoteFromFuture", Verdict.TRANSIENT),
        ("PriceOutsideBand", Verdict.TRANSIENT),
        ("InvalidQuoteSigner", Verdict.TRANSIENT),
        ("EnforcedPause", Verdict.SYSTEMIC),
        ("AccessControlUnauthorizedAccount", Verdict.SYSTEMIC),
        ("MarketDisabled", Verdict.SYSTEMIC),
        ("SelfTrade", Verdict.BUG),
        ("PriceOutOfLimits", Verdict.BUG),
        ("DustFill", Verdict.BUG),
        ("SomethingElse", Verdict.UNKNOWN),
    ],
)
def test_classification(name: str, verdict: Verdict) -> None:
    assert classify(RevertInfo(name)) is verdict


def test_no_revert_info_is_unknown_and_never_blames_an_order() -> None:
    assert classify(None) is Verdict.UNKNOWN


def test_a_cancel_race_is_never_a_retry_class() -> None:
    assert classify(RevertInfo("OrderIsCancelled")) not in (Verdict.TRANSIENT, Verdict.UNKNOWN)
