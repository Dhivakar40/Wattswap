"""Channel names: strict parsing, per-kind access rules and Redis key layout.

Every Redis key a channel owns carries the hash tag `{chan:<channel>}`, so all of a channel's keys
map to one cluster slot and the publish script stays valid on Redis Cluster.
"""

import re
from dataclasses import dataclass
from enum import StrEnum

MARKET_ID = re.compile(r"^0x[0-9a-f]{64}$")
METER_ID = MARKET_ID
ADDRESS = re.compile(r"^0x[0-9a-f]{40}$")


class Kind(StrEnum):
    PRICE = "price"
    FORECAST = "forecast"
    ORDERBOOK = "orderbook"
    TRADES = "trades"
    USER = "user"
    METER = "meter"


PUBLIC_KINDS = frozenset({Kind.PRICE, Kind.FORECAST, Kind.ORDERBOOK, Kind.TRADES})

_ARG_PATTERN = {
    Kind.PRICE: MARKET_ID,
    Kind.FORECAST: MARKET_ID,
    Kind.ORDERBOOK: MARKET_ID,
    Kind.TRADES: MARKET_ID,
    Kind.USER: ADDRESS,
    Kind.METER: METER_ID,
}


@dataclass(frozen=True)
class Channel:
    kind: Kind
    arg: str  # lowercase market id / address / meter id

    @property
    def name(self) -> str:
        return f"{self.kind.value}:{self.arg}"

    @property
    def is_public(self) -> bool:
        return self.kind in PUBLIC_KINDS


@dataclass(frozen=True)
class Retention:
    max_len: int
    max_age_s: int


# Defaults (plan decision D1). `configure_retention` overrides them from Settings at startup.
_RETENTION: dict[Kind, Retention] = {
    Kind.PRICE: Retention(500, 3600),
    Kind.FORECAST: Retention(50, 3600),
    Kind.ORDERBOOK: Retention(5000, 600),
    Kind.TRADES: Retention(5000, 600),
    Kind.USER: Retention(1000, 1800),
    Kind.METER: Retention(1000, 1800),
}


def configure_retention(overrides: dict[Kind, Retention]) -> None:
    _RETENTION.update(overrides)


def parse(name: str) -> Channel | None:
    """The channel for `name`, or None. Input is normalised to lowercase."""
    kind_s, sep, arg = name.partition(":")
    if not sep or len(name) > 100:
        return None
    try:
        kind = Kind(kind_s)
    except ValueError:
        return None
    arg = arg.lower()
    if not _ARG_PATTERN[kind].fullmatch(arg):
        return None
    return Channel(kind, arg)


def kind_of(name: str) -> Kind:
    return Kind(name.partition(":")[0])


def retention_for(name: str) -> Retention:
    return _RETENTION[kind_of(name)]


def tag(name: str) -> str:
    return f"{{chan:{name}}}"


def seq_key(name: str) -> str:
    return f"{tag(name)}:seq"


def stream_key(name: str) -> str:
    return f"{tag(name)}:stream"


def latest_key(name: str) -> str:
    return f"{tag(name)}:latest"
