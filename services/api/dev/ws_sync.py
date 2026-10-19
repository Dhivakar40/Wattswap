"""Reference implementation of the snapshot + delta protocol (docs/api.md, "Staying consistent").

Transport-agnostic and synchronous so it can be unit-tested; the Phase 6 TypeScript client mirrors
it. One `ChannelSync` tracks one channel:

    syncing --load_snapshot--> live --gap / epoch change / resync--> syncing

Rules:
  * While syncing, deltas are buffered, never applied.
  * A snapshot carries the channel's (epoch, seq) read BEFORE its data. Buffered deltas of that
    epoch with seq <= the snapshot seq are already reflected in it and are dropped; the rest must
    continue at snapshot+1. Buffered deltas of any OTHER epoch are discarded.
  * Live: same epoch and seq == last + 1 applies; seq <= last is a duplicate and is ignored; seq >
    last + 1 is a gap. A different epoch means the server's channel state was lost and its seqs
    restarted (a Redis reset): nothing may be compared by seq any more, so re-snapshot.
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
    EPOCH_CHANGED = "epoch_changed"  # caller must re-sync


class ChannelSync[S, D]:
    def __init__(self, apply: Callable[[S, D], S]) -> None:
        self._apply = apply
        self.state: S | None = None
        self.epoch: str | None = None
        self.last_seq: int | None = None
        self._buffer: dict[tuple[str, int], D] = {}
        self.syncing = True
        self.gaps = 0
        self.epoch_changes = 0

    def resume_point(self) -> dict[str, object] | None:
        """The `resume` entry to send on reconnect, or None if there is nothing to resume from."""
        if self.syncing or self.epoch is None or self.last_seq is None:
            return None
        return {"epoch": self.epoch, "seq": self.last_seq}

    def on_message(self, epoch: str, seq: int, data: D) -> Outcome:
        if self.syncing:
            self._buffer[(epoch, seq)] = data
            return Outcome.BUFFERED
        assert self.state is not None and self.last_seq is not None
        if epoch != self.epoch:
            self.epoch_changes += 1
            self.syncing = True
            self._buffer = {(epoch, seq): data}
            return Outcome.EPOCH_CHANGED
        if seq <= self.last_seq:
            return Outcome.DUPLICATE
        if seq != self.last_seq + 1:
            self.gaps += 1
            self.syncing = True
            self._buffer = {(epoch, seq): data}
            return Outcome.GAP
        self.state = self._apply(self.state, data)
        self.last_seq = seq
        return Outcome.APPLIED

    def on_resync(self) -> None:
        """The server sent a `resync` frame: the log cannot cover what was missed."""
        self.syncing = True
        self._buffer.clear()

    def load_snapshot(self, epoch: str, snapshot_seq: int, snapshot: S) -> Outcome:
        """Install a REST snapshot, then replay buffered deltas newer than it."""
        self.state, self.epoch, self.last_seq = snapshot, epoch, snapshot_seq
        mine = {seq: d for (e, seq), d in self._buffer.items() if e == epoch}
        if self._buffer and next(reversed(self._buffer))[0] != epoch:
            # The newest message belongs to a different epoch than this snapshot: the snapshot
            # predates a reset. Fetch again.
            self.epoch_changes += 1
            return Outcome.EPOCH_CHANGED
        for seq in sorted(mine):
            if seq <= self.last_seq:
                continue
            if seq != self.last_seq + 1:
                self.gaps += 1
                self._buffer = {(epoch, s): d for s, d in mine.items() if s >= seq}
                return Outcome.GAP  # snapshot was older than the buffered stream: fetch again
            self.state = self._apply(self.state, mine[seq])
            self.last_seq = seq
        self._buffer.clear()
        self.syncing = False
        return Outcome.APPLIED
