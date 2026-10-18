"""Signature acceptance rules (mirror of OZ ECDSA.tryRecover) and hash/sign round trips."""

from typing import Any

import pytest
from eth_account import Account
from eth_keys.datatypes import PrivateKey
from hexbytes import HexBytes
from hypothesis import given
from hypothesis import strategies as st

from app.chain import orders
from app.chain.orders import OrderData, OrderFormatError

EXCHANGE = "0x" + "e1" * 20
KEY = "0x" + "11" * 32
MARKET = "0x" + "aa" * 32


def _digest(amount: int = 1000) -> bytes:
    o = OrderData(
        Account.from_key(KEY).address,
        MARKET,
        0,
        amount,
        150_000,
        2_000_000_000,
        1,
        "0x" + "00" * 32,
    )
    return orders.order_digest(31337, EXCHANGE, o)


def _sign(digest: bytes) -> tuple[int, int, int]:
    sig = PrivateKey(HexBytes(KEY)).sign_msg_hash(digest)
    return sig.v + 27, sig.r, sig.s


def _pack(v: int, r: int, s: int) -> str:
    return "0x" + (r.to_bytes(32, "big") + s.to_bytes(32, "big") + bytes([v])).hex()


def test_valid_signature_recovers_the_signer() -> None:
    d = _digest()
    assert orders.recover_signer(d, _pack(*_sign(d))) == Account.from_key(KEY).address


def test_high_s_signature_is_rejected_even_though_it_recovers() -> None:
    d = _digest()
    v, r, s = _sign(d)
    flipped = _pack(55 - v, r, orders.SECP256K1_N - s)
    with pytest.raises(OrderFormatError, match="high-s"):
        orders.recover_signer(d, flipped)


@pytest.mark.parametrize("v", [0, 1, 26, 29, 255])
def test_bad_v_is_rejected(v: int) -> None:
    d = _digest()
    _, r, s = _sign(d)
    with pytest.raises(OrderFormatError, match="v must be"):
        orders.recover_signer(d, _pack(v, r, s))


def test_compact_and_odd_lengths_are_rejected() -> None:
    d = _digest()
    with pytest.raises(OrderFormatError, match="compact"):
        orders.recover_signer(d, "0x" + "11" * 64)
    for bad in ("0x", "0x" + "11" * 66, "0x" + "11" * 10, "not hex"):
        with pytest.raises(OrderFormatError):
            orders.recover_signer(d, bad)


def test_zero_r_or_s_is_rejected() -> None:
    d = _digest()
    with pytest.raises(OrderFormatError):
        orders.recover_signer(d, _pack(27, 0, 1))
    with pytest.raises(OrderFormatError):
        orders.recover_signer(d, _pack(27, 1, 0))


def test_tampered_order_recovers_a_different_address() -> None:
    sig = _pack(*_sign(_digest(1000)))
    assert orders.recover_signer(_digest(1001), sig) != Account.from_key(KEY).address


@given(
    amount=st.integers(0, 2**256 - 1),
    price=st.integers(0, 2**256 - 1),
    nonce=st.integers(0, 2**256 - 1),
    side=st.sampled_from([0, 1]),
    salt=st.binary(min_size=32, max_size=32),
)
def test_hand_rolled_digest_equals_eth_account_for_any_order(
    amount: int, price: int, nonce: int, side: int, salt: bytes
) -> None:
    from eth_account.messages import encode_typed_data
    from eth_utils import keccak

    o = OrderData(
        Account.from_key(KEY).address, MARKET, side, amount, price, 2**64, nonce, "0x" + salt.hex()
    )
    signable = encode_typed_data(full_message=orders.order_typed_data(31337, EXCHANGE, o))
    expected = keccak(b"\x19" + signable.version + signable.header + signable.body)
    assert orders.order_digest(31337, EXCHANGE, o) == expected
    msg: Any = Account.sign_message(signable, KEY)
    assert (
        orders.recover_signer(expected, "0x" + msg.signature.hex()) == Account.from_key(KEY).address
    )
