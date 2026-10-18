"""Signed price quotes: EIP-712 `PriceQuote` in the WattSwapExchange domain.

The exchange (not the oracle) verifies a quote: it recovers the signer, requires the
`PRICER_ROLE` on the PriceOracle, and bounds the quote's age by `oracle.maxStaleness()`.
Everything signed here is an `int`: a float or Decimal is refused outright.
"""

from dataclasses import dataclass
from typing import Any

from eth_account import Account
from eth_account.messages import encode_typed_data
from eth_utils import to_checksum_address
from hexbytes import HexBytes

QUOTE_TYPES: dict[str, list[dict[str, str]]] = {
    "EIP712Domain": [
        {"name": "name", "type": "string"},
        {"name": "version", "type": "string"},
        {"name": "chainId", "type": "uint256"},
        {"name": "verifyingContract", "type": "address"},
    ],
    "PriceQuote": [
        {"name": "marketId", "type": "bytes32"},
        {"name": "price", "type": "uint256"},
        {"name": "timestamp", "type": "uint256"},
    ],
}
DOMAIN_NAME = "WattSwapExchange"
DOMAIN_VERSION = "1"
UINT256_MAX = 2**256 - 1


@dataclass(frozen=True)
class PriceQuote:
    market_id: str  # 0x-prefixed bytes32
    price: int  # mUSDC base units (6 decimals) per kWh
    timestamp: int  # unix seconds, chain clock

    def __post_init__(self) -> None:
        for name in ("price", "timestamp"):
            value = getattr(self, name)
            if type(value) is not int:  # rejects float, Decimal, bool, numpy ints, ...
                raise TypeError(f"PriceQuote.{name} must be an int, got {type(value).__name__}")
            if not 0 < value <= UINT256_MAX:
                raise ValueError(f"PriceQuote.{name} out of range: {value}")
        if len(HexBytes(self.market_id)) != 32:
            raise ValueError("market_id must be 32 bytes")


@dataclass(frozen=True)
class SignedQuote:
    quote: PriceQuote
    signature: str
    signer: str

    def to_json(self) -> dict[str, Any]:
        return {
            "marketId": self.quote.market_id,
            "price": self.quote.price,
            "timestamp": self.quote.timestamp,
            "signature": self.signature,
            "signer": self.signer,
        }


def quote_typed_data(chain_id: int, exchange_address: str, q: PriceQuote) -> dict[str, Any]:
    return {
        "types": QUOTE_TYPES,
        "primaryType": "PriceQuote",
        "domain": {
            "name": DOMAIN_NAME,
            "version": DOMAIN_VERSION,
            "chainId": chain_id,
            "verifyingContract": to_checksum_address(exchange_address),
        },
        "message": {
            "marketId": HexBytes(q.market_id),
            "price": q.price,
            "timestamp": q.timestamp,
        },
    }


def sign_quote(
    private_key: str, chain_id: int, exchange_address: str, q: PriceQuote
) -> SignedQuote:
    """Deterministic (RFC 6979) signature, so identical quotes sign identically."""
    message = encode_typed_data(full_message=quote_typed_data(chain_id, exchange_address, q))
    signed = Account.sign_message(message, private_key)
    return SignedQuote(q, "0x" + signed.signature.hex(), Account.from_key(private_key).address)


def recover_quote_signer(
    chain_id: int, exchange_address: str, q: PriceQuote, signature: str
) -> str:
    message = encode_typed_data(full_message=quote_typed_data(chain_id, exchange_address, q))
    return str(Account.recover_message(message, signature=signature))
