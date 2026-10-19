"""Wire protocol of /ws: inbound ops, outbound control frames, close codes.

Data frames are the stored envelope `{type, channel, seq, ts, data}` forwarded verbatim from Redis.
Control frames carry no `seq` (`subscribed`, `unsubscribed`, `resync`, `error`, `ping`, `pong`);
they echo the client's request `id` as `ref`.
"""

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter


class Close:
    """Close codes. 1xxx are standard; 4xxx are WattSwap-specific (documented in docs/api.md)."""

    NORMAL = 1000
    GOING_AWAY = 1001  # server shutting down: reconnect and resume
    POLICY = 1008  # handshake refused (Origin); sent before accept => HTTP 403
    BAD_MESSAGE = 4400
    SESSION_ENDED = 4401  # JWT expired or logged out
    HEARTBEAT_TIMEOUT = 4408
    SLOW_CONSUMER = 4409
    TOO_LARGE = 4413
    RATE_LIMITED = 4429


class _Inbound(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: Annotated[str, Field(max_length=64)] | None = None


class Subscribe(_Inbound):
    op: Literal["subscribe"]
    channels: Annotated[
        list[Annotated[str, Field(max_length=100)]], Field(min_length=1, max_length=50)
    ]
    # channel -> last seq the client applied; the server replays what it missed
    resume: dict[Annotated[str, Field(max_length=100)], Annotated[int, Field(ge=0)]] | None = None


class Unsubscribe(_Inbound):
    op: Literal["unsubscribe"]
    channels: Annotated[
        list[Annotated[str, Field(max_length=100)]], Field(min_length=1, max_length=50)
    ]


class Ping(_Inbound):
    op: Literal["ping"]


class Pong(_Inbound):
    op: Literal["pong"]


Inbound = Annotated[Subscribe | Unsubscribe | Ping | Pong, Field(discriminator="op")]
INBOUND: TypeAdapter[Subscribe | Unsubscribe | Ping | Pong] = TypeAdapter(Inbound)


class SubscribedChannel(BaseModel):
    channel: str
    seq: int  # head seq when the subscription became live: the live stream continues at seq + 1
    replayed: int = 0  # messages replayed before this frame (resume only)


class Subscribed(BaseModel):
    type: Literal["subscribed"] = "subscribed"
    ref: str | None = None
    channels: list[SubscribedChannel]


class Unsubscribed(BaseModel):
    type: Literal["unsubscribed"] = "unsubscribed"
    ref: str | None = None
    channels: list[str]


class Resync(BaseModel):
    """The client cannot be brought up to date from the log: re-fetch the REST snapshot, then
    apply live messages with seq > snapshot seq."""

    type: Literal["resync"] = "resync"
    channel: str
    reason: Literal["too_old", "ahead_of_head", "too_many", "overflow"]
    seq: int  # head seq now


ErrorCode = Literal[
    "bad_channel",
    "unknown_market",
    "unauthorized",
    "forbidden",
    "too_many_subscriptions",
    "rate_limited",
    "bad_message",
]


class Error(BaseModel):
    type: Literal["error"] = "error"
    code: ErrorCode
    message: str
    ref: str | None = None
    channel: str | None = None


class PingFrame(BaseModel):
    type: Literal["ping"] = "ping"
    ts: int


class PongFrame(BaseModel):
    type: Literal["pong"] = "pong"
    ref: str | None = None


def dump(frame: BaseModel) -> str:
    return frame.model_dump_json(exclude_none=True)
