"""Reference implementation of the snapshot + delta protocol (docs/api.md, "Staying consistent").

Transport-agnostic and synchronous so it can be unit-tested; the Phase 6 TypeScript client mirrors
it. One `ChannelSync` tracks one channel:

    syncing --load_snapshot--> live --gap / resync--> syncing

Rules:
  * While syncing, deltas are buffered, never applied.
  * A snapshot carries the channel seq read BEFORE its data. Buffered deltas with seq <= that seq
    are already reflected in the snapshot and are dropped; the rest must continue at snapshot+1.
  * Live: seq == last + 1 applies; seq <= last is a duplicate and is ignored; seq > last + 1 is a
    gap, which re-enters `syncing` (the caller re-fetches the snapshot or resumes).
Deltas MUST be idempotent (state-replacing or upserts keyed by id), because a delta may also be
contained in the snapshot it follows.
"""

from collections.abc import Callable
from enum import StrEnum


class Outcome(StrEnum):
    APPLIED = "applied"
    BUFFERED = "buffered"
    DUPLICATE = "duplicate"
    GAP = "gap"  # caller must re-sync
    SNAPSHOT_NEEDED = "snapshot_needed"


class ChannelSync[S, D]:
    def __init__(self, apply: Callable[[S, D], S]) -> None:
        self._apply = apply
        self.state: S | None = None
        self.last_seq: int | None = None
        self._buffer: dict[int, D] = {}
        self.syncing = True
        self.gaps = 0

    def on_message(self, seq: int, data: D) -> Outcome:
        if self.syncing:
            self._buffer[seq] = data
            return Outcome.BUFFERED
        assert self.state is not None and self.last_seq is not None
        if seq <= self.last_seq:
            return Outcome.DUPLICATE
        if seq != self.last_seq + 1:
            self.gaps += 1
            self.syncing = True
            self._buffer = {seq: data}
            return Outcome.GAP
        self.state = self._apply(self.state, data)
        self.last_seq = seq
        return Outcome.APPLIED

    def on_resync(self) -> None:
        """The server sent a `resync` frame: the log cannot cover what was missed."""
        self.syncing = True
        self._buffer.clear()

    def load_snapshot(self, snapshot_seq: int, snapshot: S) -> Outcome:
        """Install a REST snapshot, then replay buffered deltas newer than it."""
        self.state, self.last_seq = snapshot, snapshot_seq
        for seq in sorted(self._buffer):
            if seq <= self.last_seq:
                continue
            if seq != self.last_seq + 1:
                self.gaps += 1
                self._buffer = {s: d for s, d in self._buffer.items() if s >= seq}
                return Outcome.GAP  # snapshot was older than the buffered stream: fetch again
            self.state = self._apply(self.state, self._buffer[seq])
            self.last_seq = seq
        self._buffer.clear()
        self.syncing = False
        return Outcome.APPLIED
