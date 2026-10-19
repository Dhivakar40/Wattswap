/**
 * Snapshot + delta consistency for ONE channel. A line-for-line port of the reference
 * `services/api/dev/ws_sync.py` (docs/api.md, "Staying consistent"); both are driven by
 * `packages/shared/vectors/ws-sync.json` so they cannot drift apart.
 *
 *     syncing --loadSnapshot--> live --gap / epoch change / resync--> syncing
 *
 * - While syncing, deltas are buffered, never applied.
 * - A snapshot carries the channel's (epoch, seq) read BEFORE its data. Buffered deltas of that epoch
 *   with seq <= the snapshot seq are already in it and are dropped; the rest must continue at snapshot+1.
 *   Buffered deltas of any other epoch are discarded.
 * - Live: same epoch and seq == last + 1 applies; seq <= last is a duplicate; seq > last + 1 is a gap.
 *   A different epoch means the server's channel state was lost and seqs restarted: never compare seqs
 *   across epochs, re-snapshot.
 * Deltas MUST be idempotent (state-replacing or upserts keyed by id).
 */

export type Outcome = "applied" | "buffered" | "duplicate" | "gap" | "epoch_changed";

export interface ResumePoint {
  epoch: string;
  seq: number;
}

export class ChannelSync<S, D> {
  state: S | null = null;
  epoch: string | null = null;
  lastSeq: number | null = null;
  syncing = true;
  gaps = 0;
  epochChanges = 0;
  /** Insertion-ordered like a Python dict; the newest entry decides epoch staleness. */
  private buffer = new Map<string, { epoch: string; seq: number; data: D }>();

  constructor(private readonly apply: (state: S, delta: D) => S) {}

  /** The `resume` entry to send on reconnect, or null if there is nothing to resume from. */
  resumePoint(): ResumePoint | null {
    if (this.syncing || this.epoch === null || this.lastSeq === null) return null;
    return { epoch: this.epoch, seq: this.lastSeq };
  }

  onMessage(epoch: string, seq: number, data: D): Outcome {
    if (this.syncing) {
      this.put(epoch, seq, data);
      return "buffered";
    }
    if (this.state === null || this.lastSeq === null) throw new Error("live without a snapshot");
    if (epoch !== this.epoch) {
      this.epochChanges += 1;
      this.syncing = true;
      this.buffer = new Map();
      this.put(epoch, seq, data);
      return "epoch_changed";
    }
    if (seq <= this.lastSeq) return "duplicate";
    if (seq !== this.lastSeq + 1) {
      this.gaps += 1;
      this.syncing = true;
      this.buffer = new Map();
      this.put(epoch, seq, data);
      return "gap";
    }
    this.state = this.apply(this.state, data);
    this.lastSeq = seq;
    return "applied";
  }

  /** The server sent a `resync` frame: the log cannot cover what was missed. */
  onResync(): void {
    this.syncing = true;
    this.buffer.clear();
  }

  /** Install a REST snapshot, then replay buffered deltas newer than it. */
  loadSnapshot(epoch: string, snapshotSeq: number, snapshot: S): Outcome {
    this.state = snapshot;
    this.epoch = epoch;
    this.lastSeq = snapshotSeq;
    const entries = [...this.buffer.values()];
    const newest = entries[entries.length - 1];
    if (newest !== undefined && newest.epoch !== epoch) {
      // The newest message belongs to a different epoch than this snapshot: the snapshot predates a
      // reset. Fetch again.
      this.epochChanges += 1;
      return "epoch_changed";
    }
    const mine = entries.filter((e) => e.epoch === epoch).sort((a, b) => a.seq - b.seq);
    for (const entry of mine) {
      if (entry.seq <= snapshotSeq) continue;
      if (this.lastSeq === null || entry.seq !== this.lastSeq + 1) {
        this.gaps += 1;
        // Snapshot was older than the buffered stream: keep what is newer and fetch again.
        this.buffer = new Map(
          mine.filter((e) => e.seq >= entry.seq).map((e) => [this.key(e.epoch, e.seq), e]),
        );
        return "gap";
      }
      this.state = this.apply(this.state, entry.data);
      this.lastSeq = entry.seq;
    }
    this.buffer.clear();
    this.syncing = false;
    return "applied";
  }

  private key(epoch: string, seq: number): string {
    return `${epoch}\u0000${seq}`;
  }

  private put(epoch: string, seq: number, data: D): void {
    this.buffer.set(this.key(epoch, seq), { epoch, seq, data });
  }
}
