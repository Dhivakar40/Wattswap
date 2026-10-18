"""Request/response models for the v1 REST API."""

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.common import Bytes32Hex, EthAddress, SignatureHex, Uint


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


# ------------------------------------------------------------------------------- auth
class NonceOut(BaseModel):
    nonce: str
    expires_in: int


class LoginIn(_Strict):
    message: str = Field(min_length=1, max_length=4096)
    signature: str = Field(min_length=1, max_length=1024)


class UserOut(BaseModel):
    id: uuid.UUID
    address: str
    role: Literal["user", "admin"]


# ----------------------------------------------------------------------------- meters
class MeterCreate(_Strict):
    label: str = Field(min_length=1, max_length=80)
    device_signer: EthAddress
    rated_capacity_w: int = Field(ge=1, le=10_000_000)
    lat: float = Field(ge=-90, le=90)
    lon: float = Field(ge=-180, le=180)


class MeterPatch(_Strict):
    label: str | None = Field(default=None, min_length=1, max_length=80)
    active: bool | None = None


class MeterOut(BaseModel):
    id: uuid.UUID
    meter_id: str
    owner: str
    device_signer: str
    label: str
    rated_capacity_w: int
    lat: float
    lon: float
    active: bool
    is_simulated: bool
    register_status: Literal["pending", "confirmed", "failed"]
    register_tx_hash: str | None
    register_error: str | None
    minted_wh: int
    created_at: datetime


# --------------------------------------------------------------------------- readings
class ReadingIn(_Strict):
    meter_id: Bytes32Hex
    reading_id: Bytes32Hex
    amount_wh: int = Field(ge=1, lt=2**63)
    interval_start: Uint
    interval_seconds: int = Field(ge=1, le=7 * 86_400)
    signature: SignatureHex
    # Informational only and NOT covered by the signature: lets the receiver label the source.
    simulated: bool = False


class MintOut(BaseModel):
    status: Literal["pending", "submitted", "confirmed", "failed"]
    tx_hash: str | None
    block_number: int | None
    attempts: int
    failure_reason: str | None
    confirmed_at: datetime | None


class ReadingOut(BaseModel):
    reading_id: str
    meter_id: str
    amount_wh: int
    interval_start: int
    interval_seconds: int
    received_at: datetime
    mint: MintOut | None


class ReadingAccepted(BaseModel):
    reading: ReadingOut
    duplicate: bool


class ReadingsPage(BaseModel):
    items: list[ReadingOut]
    next_before: int | None


class MintItem(BaseModel):
    reading_id: str
    meter_id: str
    amount_wh: int
    interval_start: int
    interval_seconds: int
    mint: MintOut


class MintsPage(BaseModel):
    items: list[MintItem]
    next_before: datetime | None


# ---------------------------------------------------------------------------- config
class PublicConfig(BaseModel):
    chain_id: int
    energy_token: str
    meter_registry: str
    reading_interval_seconds: int
    min_interval_seconds: int
    max_interval_seconds: int
    safety_bps: int
    simulated_meters: Literal[True] = True
    note: str
