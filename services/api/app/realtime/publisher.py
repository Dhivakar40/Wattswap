"""Atomic publish: allocate seq + retain in a stream + fan out, in ONE Redis script.

The Phase 4 publisher did `INCR` then `PUBLISH` as two round trips, so two concurrent publishers
could deliver seq 6 before seq 5 and a client would read that as a gap. Here the sequence number
is allocated, the envelope retained (`XADD`, id = `<seq>-0`) and broadcast (`PUBLISH`) inside a
single script, so delivery order on a channel always equals seq order.

Retention is by count (exact `MAXLEN`) and by age (the oldest entries are deleted once older than the
window), both per channel kind. Keys are hash-tagged per channel (see `channels`).
"""

import json
import time
from dataclasses import dataclass
from typing import Any

import redis.asyncio as aioredis

from app.realtime import channels

# KEYS: seq, stream, latest
# ARGV: type_json, channel_json, ts, data_json, max_len, max_age_ms, now_ms, set_latest, channel
_SCRIPT = """
local seq = redis.call('INCR', KEYS[1])
local body = '{"type":' .. ARGV[1] .. ',"channel":' .. ARGV[2] .. ',"seq":' .. seq
  .. ',"ts":' .. ARGV[3] .. ',"data":' .. ARGV[4] .. '}'
redis.call('XADD', KEYS[2], 'MAXLEN', ARGV[5], seq .. '-0', 'm', body, 't', ARGV[7])
local cutoff = tonumber(ARGV[7]) - tonumber(ARGV[6])
for _ = 1, 100 do
  local oldest = redis.call('XRANGE', KEYS[2], '-', '+', 'COUNT', 1)
  if #oldest == 0 or tonumber(oldest[1][2][4]) >= cutoff then break end
  redis.call('XDEL', KEYS[2], oldest[1][1])
end
if ARGV[8] == '1' then redis.call('SET', KEYS[3], body) end
local receivers = redis.call('PUBLISH', ARGV[9], body)
return {seq, receivers}
"""


@dataclass(frozen=True)
class Published:
    seq: int
    receivers: int


def _dumps(value: object) -> str:
    return json.dumps(value, separators=(",", ":"))


async def publish(
    redis: aioredis.Redis,
    channel: str,
    type_: str,
    data: dict[str, Any],
    *,
    ts: int | None = None,
    keep_latest: bool = False,
) -> Published:
    """Publish one message and return its seq. `keep_latest` also stores the envelope at the
    channel's `latest` key inside the same script, so it always matches the seq."""
    ret = channels.retention_for(channel)
    now_ms = int(time.time() * 1000)
    script = redis.register_script(_SCRIPT)
    result = await script(
        keys=[
            channels.seq_key(channel),
            channels.stream_key(channel),
            channels.latest_key(channel),
        ],
        args=[
            _dumps(type_),
            _dumps(channel),
            ts if ts is not None else now_ms // 1000,
            _dumps(data),
            ret.max_len,
            ret.max_age_s * 1000,
            now_ms,
            "1" if keep_latest else "0",
            channel,
        ],
    )
    return Published(int(result[0]), int(result[1]))
