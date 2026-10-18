"""EIP-712 `Order` hashing and signature checks, matching WattSwapExchange byte for byte.

Two independent implementations of the digest live here and are cross-checked in tests:
`order_digest` (hand-rolled `keccak(0x1901 ‖ domainSeparator ‖ structHash)` with `eth_abi`) and
`order_typed_data` (for `eth_account` / wallets). Both are pinned to the Foundry-generated vectors
in `packages/shared/vectors/eip712.json` (CI-gated, tests/unit/test_order_vectors.py).

Signature acceptance mirrors OpenZeppelin `ECDSA.tryRecover` as used by `SignatureChecker`, so an
order this module accepts cannot be rejected on-chain for signature-format reasons: exactly 65
bytes, `v` in {27, 28}, and a low `s` (high-s is rejected). EIP-2098 64-byte compact signatures
are valid on-chain but deliberately rejected here in v1.
"""

from dataclasses import dataclass
from typing import Any

from eth_abi import encode
from eth_keys.datatypes import Signature
from eth_utils import keccak, to_checksum_address
from hexbytes import HexBytes

BUY = 0
SELL = 1
UINT256_MAX = 2**256 - 1
UINT8_MAX = 255
# secp256k1 group order; OZ rejects s > N/2 (signature malleability).
SECP256K1_N = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141
HALF_N = SECP256K1_N // 2

DOMAIN_NAME = "WattSwapExchange"
DOMAIN_VERSION = "1"

ORDER_TYPE_STRING = (
    "Order(address maker,bytes32 marketId,uint8 side,uint256 amountWh,uint256 limitPrice,"
    "uint256 expiry,uint256 nonce,bytes32 salt)"
)
ORDER_TYPEHASH = keccak(text=ORDER_TYPE_STRING)
DOMAIN_TYPEHASH = keccak(
    text="EIP712Domain(string name,string version,uint256 chainId,address verifyingContract)"
)

ORDER_TYPES: dict[str, list[dict[str, str]]] = {
    "EIP712Domain": [
        {"name": "name", "type": "string"},
        {"name": "version", "type": "string"},
        {"name": "chainId", "type": "uint256"},
        {"name": "verifyingContract", "type": "address"},
    ],
    "Order": [
        {"name": "maker", "type": "address"},
        {"name": "marketId", "type": "bytes32"},
        {"name": "side", "type": "uint8"},
        {"name": "amountWh", "type": "uint256"},
        {"name": "limitPrice", "type": "uint256"},
        {"name": "expiry", "type": "uint256"},
        {"name": "nonce", "type": "uint256"},
        {"name": "salt", "type": "bytes32"},
    ],
}


class OrderFormatError(ValueError):
    """The order or its signature is malformed. `code` is a stable machine-readable reason."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _bytes_n(value: str, n: int, field: str) -> bytes:
    try:
        raw = bytes(HexBytes(value))
    except (ValueError, TypeError) as exc:
        raise OrderFormatError("bad_order", f"{field} is not valid hex") from exc
    if len(raw) != n:
        raise OrderFormatError("bad_order", f"{field} must be {n} bytes")
    return raw


@dataclass(frozen=True)
class OrderData:
    """The signed fields, exactly as in `OrderLib.Order`. All integers are exact Python ints."""

    maker: str
    market_id: str  # 0x bytes32
    side: int  # 0 buy, 1 sell
    amount_wh: int
    limit_price: int  # mUSDC base units (6 dp) per kWh
    expiry: int
    nonce: int
    salt: str  # 0x bytes32

    def __post_init__(self) -> None:
        for name in ("side", "amount_wh", "limit_price", "expiry", "nonce"):
            value = getattr(self, name)
            if type(value) is not int:  # rejects float / Decimal / bool
                raise OrderFormatError("bad_order", f"{name} must be an integer")
            if not 0 <= value <= UINT256_MAX:
                raise OrderFormatError("bad_order", f"{name} out of uint256 range")
        if self.side not in (BUY, SELL):
            raise OrderFormatError("bad_order", "side must be 0 (buy) or 1 (sell)")
        _bytes_n(self.maker, 20, "maker")
        _bytes_n(self.market_id, 32, "marketId")
        _bytes_n(self.salt, 32, "salt")


def domain_separator(chain_id: int, exchange_address: str) -> bytes:
    return keccak(
        encode(
            ["bytes32", "bytes32", "bytes32", "uint256", "address"],
            [
                DOMAIN_TYPEHASH,
                keccak(text=DOMAIN_NAME),
                keccak(text=DOMAIN_VERSION),
                chain_id,
                to_checksum_address(exchange_address),
            ],
        )
    )


def struct_hash(o: OrderData) -> bytes:
    return keccak(
        encode(
            [
                "bytes32",
                "address",
                "bytes32",
                "uint8",
                "uint256",
                "uint256",
                "uint256",
                "uint256",
                "bytes32",
            ],
            [
                ORDER_TYPEHASH,
                to_checksum_address(o.maker),
                HexBytes(o.market_id),
                o.side,
                o.amount_wh,
                o.limit_price,
                o.expiry,
                o.nonce,
                HexBytes(o.salt),
            ],
        )
    )


def order_digest(chain_id: int, exchange_address: str, o: OrderData) -> bytes:
    """The order's identity: equals `WattSwapExchange.hashOrder(order)`."""
    return keccak(b"\x19\x01" + domain_separator(chain_id, exchange_address) + struct_hash(o))


def order_hash_hex(chain_id: int, exchange_address: str, o: OrderData) -> str:
    return "0x" + order_digest(chain_id, exchange_address, o).hex()


def order_typed_data(chain_id: int, exchange_address: str, o: OrderData) -> dict[str, Any]:
    """Full EIP-712 payload for `eth_account.messages.encode_typed_data(full_message=...)`."""
    return {
        "types": ORDER_TYPES,
        "primaryType": "Order",
        "domain": {
            "name": DOMAIN_NAME,
            "version": DOMAIN_VERSION,
            "chainId": chain_id,
            "verifyingContract": to_checksum_address(exchange_address),
        },
        "message": {
            "maker": to_checksum_address(o.maker),
            "marketId": HexBytes(o.market_id),
            "side": o.side,
            "amountWh": o.amount_wh,
            "limitPrice": o.limit_price,
            "expiry": o.expiry,
            "nonce": o.nonce,
            "salt": HexBytes(o.salt),
        },
    }


def parse_signature(signature: str) -> tuple[int, int, int]:
    """(v, r, s) after applying the exact acceptance rules of OZ `ECDSA.tryRecover` (65 bytes)."""
    try:
        raw = bytes(HexBytes(signature))
    except (ValueError, TypeError) as exc:
        raise OrderFormatError("bad_signature", "signature is not valid hex") from exc
    if len(raw) == 64:
        raise OrderFormatError(
            "bad_signature", "64-byte EIP-2098 compact signatures are not supported; send 65 bytes"
        )
    if len(raw) != 65:
        raise OrderFormatError("bad_signature", "signature must be 65 bytes (r ‖ s ‖ v)")
    r = int.from_bytes(raw[:32], "big")
    s = int.from_bytes(raw[32:64], "big")
    v = raw[64]
    if v not in (27, 28):
        raise OrderFormatError("bad_signature", "signature v must be 27 or 28")
    if s > HALF_N:
        raise OrderFormatError("bad_signature", "high-s signatures are rejected (malleable)")
    if r == 0 or s == 0 or r >= SECP256K1_N:
        raise OrderFormatError("bad_signature", "signature r/s out of range")
    return v, r, s


def recover_signer(digest: bytes, signature: str) -> str:
    """Checksummed address that signed `digest`; raises OrderFormatError if unrecoverable."""
    v, r, s = parse_signature(signature)
    try:
        sig = Signature(vrs=(v - 27, r, s))
        return str(sig.recover_public_key_from_msg_hash(digest).to_checksum_address())
    except Exception as exc:  # eth_keys raises BadSignature / ValidationError / ValueError
        raise OrderFormatError("bad_signature", "signature does not recover to an address") from exc
