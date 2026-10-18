"""Redis leader election for single-writer background loops (matcher, relayer, listener, sweeper).

Same pattern as the mint worker: whoever holds `lock:<name>` runs `step()` every `interval`; others
idle and take over when the TTL lapses. `LeaderLoop.owned()` lets a step re-verify, right before it
commits, that it still holds the lock (guards against a pause longer than the TTL).
"""

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable

import redis.asyncio as aioredis
from redis.exceptions import LockError

from app.core.metrics import LEADER

log = logging.getLogger("wattswap.leader")


class LeaderLoop:
    def __init__(
        self,
        redis: aioredis.Redis,
        name: str,
        *,
        ttl_seconds: int,
        interval_seconds: float,
        step: Callable[[], Awaitable[object]],
        max_backoff_seconds: float = 30.0,
    ) -> None:
        self.name = name
        self.redis = redis
        self.interval = interval_seconds
        self.step = step
        self.max_backoff = max_backoff_seconds
        self.lock = redis.lock(f"lock:{name}", timeout=ttl_seconds)
        self.leader = False
        self.steps = 0
        self.last_error: str | None = None

    async def owned(self) -> bool:
        """True while this instance still holds the lock (checked against Redis, not memory)."""
        try:
            return self.leader and bool(await self.lock.owned())
        except Exception:
            return False

    async def run(self, stop: asyncio.Event) -> None:
        failures = 0
        try:
            while not stop.is_set():
                pause = self.interval
                try:
                    if not self.leader:
                        self.leader = bool(await self.lock.acquire(blocking=False))
                        if self.leader:
                            log.info("%s became leader", self.name)
                    LEADER.labels(self.name).set(1 if self.leader else 0)
                    if self.leader:
                        await self.lock.reacquire()
                        started = time.monotonic()
                        await self.step()
                        self.steps += 1
                        pause = max(0.0, self.interval - (time.monotonic() - started))
                    failures = 0
                except LockError:
                    self.leader = False
                    LEADER.labels(self.name).set(0)
                    log.warning("%s lost its leader lock", self.name)
                except Exception as exc:
                    failures += 1
                    self.last_error = f"{type(exc).__name__}: {exc}"
                    pause = min(self.max_backoff, max(self.interval, 0.5 * 2**failures))
                    log.exception("%s iteration failed", self.name, extra={"retry_in_s": pause})
                try:
                    await asyncio.wait_for(stop.wait(), pause)
                except TimeoutError:
                    pass
        finally:
            if self.leader:
                try:
                    await self.lock.release()
                except LockError:
                    pass
                self.leader = False
                LEADER.labels(self.name).set(0)
