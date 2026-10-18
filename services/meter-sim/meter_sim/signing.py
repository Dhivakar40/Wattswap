"""EIP-712 signing of readings (mirrors EnergyToken.READING_TYPEHASH)."""

from dataclasses import dataclass
from typing import Any

from eth_abi import encode
from eth_account import Account
from eth_account.messages import encode_typed_data
from eth_utils import keccak, to_bytes, to_checksum_address

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


@dataclass(frozen=True)
class SignedReading:
    meter_id: str
    reading_id: str
    amount_wh: int
    interval_start: int
    interval_seconds: int
    signature: str

    def to_json(self) -> dict[str, Any]:
        return {
            "meter_id": self.meter_id,
            "reading_id": self.reading_id,
            "amount_wh": self.amount_wh,
            "interval_start": self.interval_start,
            "interval_seconds": self.interval_seconds,
            "signature": self.signature,
            # Not covered by the signature; lets the receiver label the source honestly.
            "simulated": True,
        }


def derive_reading_id(meter_id: str, interval_start: int, interval_seconds: int) -> str:
    """keccak256(abi.encode(meterId, intervalStart, intervalSeconds)): same interval, same id."""
    packed = encode(
        ["bytes32", "uint256", "uint256"],
        [to_bytes(hexstr=meter_id), interval_start, interval_seconds],
    )
    return "0x" + keccak(packed).hex()


def typed_data(
    chain_id: int,
    token_address: str,
    meter_id: str,
    reading_id: str,
    amount_wh: int,
    interval_start: int,
    interval_seconds: int,
) -> dict[str, Any]:
    return {
        "types": READING_TYPES,
        "primaryType": "Reading",
        "domain": {
            "name": "WattSwap Energy",
            "version": "1",
            "chainId": chain_id,
            "verifyingContract": to_checksum_address(token_address),
        },
        "message": {
            "meterId": to_bytes(hexstr=meter_id),
            "readingId": to_bytes(hexstr=reading_id),
            "amountWh": amount_wh,
            "intervalStart": interval_start,
            "intervalSeconds": interval_seconds,
        },
    }


def sign_with_id(
    private_key: str,
    chain_id: int,
    token_address: str,
    meter_id: str,
    reading_id: str,
    amount_wh: int,
    interval_start: int,
    interval_seconds: int,
) -> SignedReading:
    data = typed_data(
        chain_id, token_address, meter_id, reading_id, amount_wh, interval_start, interval_seconds
    )
    signed = Account.sign_message(encode_typed_data(full_message=data), private_key)
    return SignedReading(
        meter_id=meter_id,
        reading_id=reading_id,
        amount_wh=amount_wh,
        interval_start=interval_start,
        interval_seconds=interval_seconds,
        signature="0x" + signed.signature.hex(),
    )


def sign_reading(
    private_key: str,
    chain_id: int,
    token_address: str,
    meter_id: str,
    amount_wh: int,
    interval_start: int,
    interval_seconds: int,
) -> SignedReading:
    """Sign a reading using the canonical derived reading id."""
    reading_id = derive_reading_id(meter_id, interval_start, interval_seconds)
    return sign_with_id(
        private_key,
        chain_id,
        token_address,
        meter_id,
        reading_id,
        amount_wh,
        interval_start,
        interval_seconds,
    )
