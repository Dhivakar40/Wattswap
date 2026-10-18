"""Request/response models for the pricing API."""

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.common import Bytes32Hex


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RegionOut(BaseModel):
    min_lat_e6: int | None
    max_lat_e6: int | None
    min_lon_e6: int | None
    max_lon_e6: int | None


class MarketOut(BaseModel):
    market_id: str
    name: str
    enabled: bool
    is_primary: bool
    region: RegionOut
    onchain_enabled: bool | None = None  # WattSwapExchange.marketEnabled, if the chain is reachable


class MarketCreate(_Strict):
    name: str = Field(min_length=1, max_length=64)
    enabled: bool = True
    is_primary: bool = False
    min_lat_e6: int | None = Field(default=None, ge=-90_000_000, le=90_000_000)
    max_lat_e6: int | None = Field(default=None, ge=-90_000_000, le=90_000_000)
    min_lon_e6: int | None = Field(default=None, ge=-180_000_000, le=180_000_000)
    max_lon_e6: int | None = Field(default=None, ge=-180_000_000, le=180_000_000)


class MarketPatch(_Strict):
    enabled: bool | None = None
    is_primary: bool | None = None
    min_lat_e6: int | None = Field(default=None, ge=-90_000_000, le=90_000_000)
    max_lat_e6: int | None = Field(default=None, ge=-90_000_000, le=90_000_000)
    min_lon_e6: int | None = Field(default=None, ge=-180_000_000, le=180_000_000)
    max_lon_e6: int | None = Field(default=None, ge=-180_000_000, le=180_000_000)


class ConfigEntry(BaseModel):
    key: str
    kind: str
    unit: str
    description: str
    default: Any
    value: Any
    source: str  # default | global | market
    overridden: bool
    minimum: str | None
    maximum: str | None
    editable: bool
    last_change: dict[str, Any] | None = None


class ConfigOut(BaseModel):
    market_id: str | None
    scope: str
    entries: list[ConfigEntry]


class ConfigUpdate(_Strict):
    market_id: Bytes32Hex | None = Field(
        default=None, description="Market to override; omit for all markets"
    )
    values: dict[str, Any] = Field(default_factory=dict)
    reset: list[str] = Field(default_factory=list, description="Keys whose override to remove")


class BreakerResetIn(_Strict):
    market_id: Bytes32Hex
