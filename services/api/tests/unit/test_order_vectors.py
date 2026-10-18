"""CI GATE: Python reproduces every Foundry-generated EIP-712 vector exactly.

The vectors in packages/shared/vectors/eip712.json are produced by the real contracts
(contracts/test/Vectors.t.sol, which fails if the file drifts). Here the Python implementation must
reproduce, for EVERY vector: domain separator, struct hash, digest, recovered signer, and the
byte-identical (RFC 6979, low-s) signature. A vector can't be dropped silently: the exact name list
is asserted.
"""

import json
from pathlib import Path
from typing import Any

import pytest
from eth_account import Account
from eth_account.messages import encode_typed_data
from eth_keys.datatypes import PrivateKey
from eth_utils import keccak
from hexbytes import HexBytes

from app.chain import orders
from app.chain.orders import OrderData

VECTORS_PATH = (
    Path(__file__).resolve().parents[4] / "packages" / "shared" / "vectors" / "eip712.json"
)
DATA: dict[str, Any] = json.loads(VECTORS_PATH.read_text(encoding="utf-8"))
VECTORS: list[dict[str, Any]] = DATA["vectors"]

EXPECTED_NAMES = [
    "order_buy",
    "order_sell",
    "price_quote",
    "meter_reading",
    "price_quote_min_price",
    "price_quote_night",
    "price_quote_max_safe_price",
    "price_quote_second_market",
    "order_max_values",
    "order_zero_nonce_zero_salt",
    "order_leading_zero_maker",
    "order_second_market",
]
ORDER_VECTORS = [v for v in VECTORS if v["primaryType"] == "Order"]


def _key_for(name: str) -> str:
    keys = DATA["testKeys"]
    if name.startswith("price_quote"):
        return str(keys["pricer"])
    if name in ("order_buy", "order_max_values", "order_leading_zero_maker"):
        return str(keys["buyer"])
    if name in ("order_sell", "order_zero_nonce_zero_salt", "order_second_market"):
        return str(keys["seller"])
    return str(keys["device"])


def _to_python(td: dict[str, Any]) -> dict[str, Any]:
    """JSON typed data -> what eth_account wants: ints for uints, bytes for bytes32."""
    fields = {f["name"]: f["type"] for f in td["types"][td["primaryType"]]}
    message: dict[str, Any] = {}
    for key, value in td["message"].items():
        kind = fields[key]
        if kind.startswith("uint"):
            message[key] = int(value)
        elif kind.startswith("bytes"):
            message[key] = HexBytes(value)
        else:
            message[key] = value
    return {**td, "message": message}


def _order_from(v: dict[str, Any]) -> OrderData:
    m = v["typedData"]["message"]
    return OrderData(
        maker=m["maker"],
        market_id=m["marketId"],
        side=int(m["side"]),
        amount_wh=int(m["amountWh"]),
        limit_price=int(m["limitPrice"]),
        expiry=int(m["expiry"]),
        nonce=int(m["nonce"]),
        salt=m["salt"],
    )


def test_vector_set_is_exactly_the_expected_one() -> None:
    assert [v["name"] for v in VECTORS] == EXPECTED_NAMES
    assert DATA["chainId"] == 31337
    assert len(ORDER_VECTORS) == 6


@pytest.mark.parametrize("v", VECTORS, ids=lambda v: v["name"])
def test_eth_account_reproduces_digest_signer_and_signature(v: dict[str, Any]) -> None:
    signable = encode_typed_data(full_message=_to_python(v["typedData"]))
    digest = keccak(b"\x19" + signable.version + signable.header + signable.body)
    assert "0x" + digest.hex() == v["digest"]
    signed = Account.sign_message(signable, _key_for(v["name"]))
    assert "0x" + signed.signature.hex() == v["signature"]
    assert Account.recover_message(signable, signature=v["signature"]) == v["signer"]
    assert Account.from_key(_key_for(v["name"])).address == v["signer"]


@pytest.mark.parametrize("v", ORDER_VECTORS, ids=lambda v: v["name"])
def test_order_module_reproduces_every_order_vector(v: dict[str, Any]) -> None:
    domain = v["typedData"]["domain"]
    chain_id, exchange = int(domain["chainId"]), domain["verifyingContract"]
    o = _order_from(v)
    assert "0x" + orders.domain_separator(chain_id, exchange).hex() == v["domainSeparator"]
    assert "0x" + orders.struct_hash(o).hex() == v["structHash"]
    digest = orders.order_digest(chain_id, exchange, o)
    assert "0x" + digest.hex() == v["digest"]
    assert orders.order_hash_hex(chain_id, exchange, o) == v["digest"]

    # Independent route: the typed-data payload through eth_account gives the same digest.
    signable = encode_typed_data(full_message=orders.order_typed_data(chain_id, exchange, o))
    assert keccak(b"\x19" + signable.version + signable.header + signable.body) == digest

    # Byte-identical signature, accepted by our (OZ-equivalent) signature rules.
    sig = PrivateKey(HexBytes(_key_for(v["name"]))).sign_msg_hash(digest)
    raw = sig.r.to_bytes(32, "big") + sig.s.to_bytes(32, "big") + bytes([sig.v + 27])
    assert "0x" + raw.hex() == v["signature"]
    assert orders.recover_signer(digest, v["signature"]) == v["signer"]


def test_every_order_vector_is_low_s_and_65_bytes() -> None:
    for v in VECTORS:
        raw = HexBytes(v["signature"])
        assert len(raw) == 65
        assert int.from_bytes(raw[32:64], "big") <= orders.HALF_N


def test_domain_binds_chain_and_contract() -> None:
    v = ORDER_VECTORS[0]
    o = _order_from(v)
    exchange = v["typedData"]["domain"]["verifyingContract"]
    base = orders.order_digest(31337, exchange, o)
    assert orders.order_digest(1, exchange, o) != base  # replay on another chain
    other = "0x" + "11" * 20
    assert orders.order_digest(31337, other, o) != base  # replay on another deployment


def test_order_data_rejects_non_integers_and_out_of_range() -> None:
    base: dict[str, Any] = {
        "maker": "0x" + "ab" * 20,
        "market_id": "0x" + "01" * 32,
        "side": 0,
        "amount_wh": 1,
        "limit_price": 1,
        "expiry": 1,
        "nonce": 0,
        "salt": "0x" + "00" * 32,
    }
    OrderData(**base)
    for bad in (
        {"amount_wh": 1.5},
        {"amount_wh": True},
        {"limit_price": -1},
        {"nonce": 2**256},
        {"side": 2},
        {"maker": "0x1234"},
        {"salt": "0x00"},
    ):
        with pytest.raises(orders.OrderFormatError):
            OrderData(**{**base, **bad})
