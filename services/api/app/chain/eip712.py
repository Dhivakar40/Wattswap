"""EIP-712 helpers for meter readings (mirrors EnergyToken.READING_TYPEHASH)."""

from dataclasses import dataclass
from typing import Any

from eth_abi import encode
from eth_account import Account
from eth_account.messages import encode_typed_data
from eth_utils import keccak, to_checksum_address
from hexbytes import HexBytes

READING_TYPES: dict[str, list[dict[str, str]]] = {
    "EIP712Domain": [
        {"name": "name", "type": "string"},
        {"name": "version", "type": "string"},
        {"name": "chainId", "type": "uint256"},
        {"name": "verifyingContract", "type": "address"},
    ],
    "Reading": [
        {"name": "meterId", "type": "bytes32"},
        {"name": "readingId", "type": "bytes32"},
        {"name": "amountWh", "type": "uint256"},
        {"name": "intervalStart", "type": "uint256"},
        {"name": "intervalSeconds", "type": "uint256"},
    ],
}
READING_DOMAIN_NAME = "WattSwap Energy"
READING_DOMAIN_VERSION = "1"


@dataclass(frozen=True)
class ReadingData:
    meter_id: str
    reading_id: str
    amount_wh: int
    interval_start: int
    interval_seconds: int


def derive_reading_id(meter_id: str, interval_start: int, interval_seconds: int) -> str:
    """readingId = keccak256(abi.encode(meterId, intervalStart, intervalSeconds)).

    Deterministic, so re-sending the same interval is naturally idempotent.
    """
    packed = encode(
        ["bytes32", "uint256", "uint256"],
        [HexBytes(meter_id), interval_start, interval_seconds],
    )
    return "0x" + keccak(packed).hex()


def reading_typed_data(chain_id: int, token_address: str, r: ReadingData) -> dict[str, Any]:
    return {
        "types": READING_TYPES,
        "primaryType": "Reading",
        "domain": {
            "name": READING_DOMAIN_NAME,
            "version": READING_DOMAIN_VERSION,
            "chainId": chain_id,
            "verifyingContract": to_checksum_address(token_address),
        },
        "message": {
            "meterId": HexBytes(r.meter_id),
            "readingId": HexBytes(r.reading_id),
            "amountWh": r.amount_wh,
            "intervalStart": r.interval_start,
            "intervalSeconds": r.interval_seconds,
        },
    }


def recover_reading_signer(
    chain_id: int, token_address: str, r: ReadingData, signature: str
) -> str | None:
    """Address that signed the reading, or None if the signature is malformed."""
    try:
        message = encode_typed_data(full_message=reading_typed_data(chain_id, token_address, r))
        return str(Account.recover_message(message, signature=signature))
    except Exception:  # eth_account raises several unrelated types for bad signatures
        return None
