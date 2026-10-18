"""Shared validated types."""

import re
from typing import Annotated

from eth_utils import is_address, to_checksum_address
from pydantic import AfterValidator, Field

_BYTES32 = re.compile(r"^0x[0-9a-fA-F]{64}$")
_SIG = re.compile(r"^0x[0-9a-fA-F]{130}$")


def _address(v: str) -> str:
    if not is_address(v):
        raise ValueError("must be a 20-byte hex address")
    return v.lower()


def _bytes32(v: str) -> str:
    if not _BYTES32.match(v):
        raise ValueError("must be 32 bytes of 0x-prefixed hex")
    return v.lower()


def _signature(v: str) -> str:
    if not _SIG.match(v):
        raise ValueError("must be a 65-byte 0x-prefixed hex signature")
    return v.lower()


# Inputs are normalised to lowercase (the storage form); responses use checksum().
EthAddress = Annotated[str, AfterValidator(_address)]
Bytes32Hex = Annotated[str, AfterValidator(_bytes32)]
SignatureHex = Annotated[str, AfterValidator(_signature)]
Uint = Annotated[int, Field(ge=0, lt=2**63)]  # JSON-safe, fits BIGINT


def checksum(address: str) -> str:
    return to_checksum_address(address)
