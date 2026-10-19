import { beforeEach, describe, expect, it, vi } from "vitest";
import {
  backoffDelay,
  CLOSE,
  RealtimeClient,
  type ChannelConfig,
  type ConnectionStatus,
  type DataFrame,
  type Snapshot,
  type SocketLike,
  type Timers,
} from "@/lib/realtime/client";

// ---------------------------------------------------------------------------------- test doubles
class FakeSocket implements SocketLike {
  onopen: ((ev: unknown) => void) | null = null;
  onmessage: ((ev: { data: unknown }) => void) | null = null;
  onclose: ((ev: { code: number; reason?: string }) => void) | null = null;
  onerror: ((ev: unknown) => void) | null = null;
  sent: Array<Record<string, unknown>> = [];
  closedWith: number | null = null;
  send(data: string): void {
    this.sent.push(JSON.parse(data) as Record<string, unknown>);
  }
  close(code?: number): void {
    this.closedWith = code ?? 1000;
  }
  // server side helpers
  open(): void {
    this.onopen?.({});
  }
  push(frame: Record<string, unknown>): void {
    this.onmessage?.({ data: JSON.stringify(frame) });
  }
  drop(code: number): void {
    this.onclose?.({ code });
  }
  subscribes(): Array<Record<string, unknown>> {
    return this.sent.filter((f) => f.op === "subscribe");
  }
}

class FakeClock implements Timers {
  now = 0;
  private seq = 0;
  private jobs = new Map<number, { at: number; fn: () => void }>();
  setTimeout(fn: () => void, ms: number): unknown {
    const id = ++this.seq;
    this.jobs.set(id, { at: this.now + ms, fn });
    return id;
  }
  clearTimeout(h: unknown): void {
    this.jobs.delete(h as number);
  }
  pending(): number[] {
    return [...this.jobs.values()].map((j) => j.at - this.now).sort((a, b) => a - b);
  }
  async advance(ms: number): Promise<void> {
    const target = this.now + ms;
    for (;;) {
      const next = [...this.jobs.entries()]
        .filter(([, j]) => j.at <= target)
        .sort((a, b) => a[1].at - b[1].at)[0];
      if (next === undefined) break;
      this.jobs.delete(next[0]);
      this.now = next[1].at;
      next[1].fn();
      await flush();
    }
    this.now = target;
  }
}

const flush = async (): Promise<void> => {
  for (let i = 0; i < 10; i++) await Promise.resolve();
};

const CH = "orderbook:0xabc";
const E1 = "epoch-1";
const E2 = "epoch-2";

const frame = (seq: number, epoch = E1, data: unknown = seq): Record<string, unknown> => ({
  type: "orderbook",
  channel: CH,
  epoch,
  seq,
  ts: 1,
  data,
});

interface Harness {
  client: RealtimeClient;
  clock: FakeClock;
  sockets: FakeSocket[];
  snapshots: Array<Snapshot<number[]>>;
  loadSnapshot: ReturnType<typeof vi.fn>;
  states: number[][];
  statuses: ConnectionStatus[];
  errors: unknown[];
  cfg: ChannelConfig<number[]>;
  last(): FakeSocket;
}

function setup(
  opts: { random?: () => number; shouldRetry?: (e: unknown) => boolean } = {},
): Harness {
  const clock = new FakeClock();
  const sockets: FakeSocket[] = [];
  const snapshots: Array<Snapshot<number[]>> = [];
  const states: number[][] = [];
  const statuses: ConnectionStatus[] = [];
  const errors: unknown[] = [];
  const loadSnapshot = vi.fn(async () => {
    const s = snapshots.shift();
    if (s === undefined) throw new Error("no snapshot queued");
    return s;
  });
  const cfg: ChannelConfig<number[]> = {
    name: CH,
    loadSnapshot,
    apply: (state, f) => [...state, f.data as number],
    onState: (s) => states.push(s),
    onError: (e) => errors.push(e),
    ...(opts.shouldRetry ? { shouldRetry: opts.shouldRetry } : {}),
  };
  const client = new RealtimeClient({
    url: "ws://test/ws",
    createSocket: () => {
      const s = new FakeSocket();
      sockets.push(s);
      return s;
    },
    timers: clock,
    random: opts.random ?? (() => 0.5),
    onStatus: (s) => statuses.push(s),
  });
  return {
    client,
    clock,
    sockets,
    snapshots,
    loadSnapshot,
    states,
    statuses,
    errors,
    cfg,
    last: () => sockets[sockets.length - 1] as FakeSocket,
  };
}

/** Subscribe, connect, answer `subscribed`, load snapshot (epoch E1, seq 10). */
async function live(h: Harness): Promise<FakeSocket> {
  h.snapshots.push({ data: [0], epoch: E1, seq: 10 });
  h.client.subscribe(h.cfg);
  const s = h.last();
  s.open();
  s.push({
    type: "subscribed",
    ref: "1",
    channels: [{ channel: CH, epoch: E1, seq: 10, replayed: 0 }],
  });
  await flush();
  return s;
}

beforeEach(() => {
  vi.restoreAllMocks();
});

// ------------------------------------------------------------------------------------------ tests
describe("subscribe and snapshot", () => {
  it("subscribes on open, buffers deltas until the snapshot, then applies only newer ones", async () => {
    const h = setup();
    h.snapshots.push({ data: [0], epoch: E1, seq: 10 });
    h.client.subscribe(h.cfg);
    const s = h.last();
    expect(h.client.getStatus()).toBe("connecting");
    s.open();
    expect(s.subscribes()).toEqual([{ op: "subscribe", channels: [CH], id: "1" }]); // no resume yet
    s.push(frame(10)); // buffered, already in the snapshot
    s.push(frame(11)); // buffered, newer
    s.push({
      type: "subscribed",
      ref: "1",
      channels: [{ channel: CH, epoch: E1, seq: 9, replayed: 0 }],
    });
    await flush();
    expect(h.states).toEqual([[0, 11]]);
    expect(h.client.getStatus()).toBe("live");
    s.push(frame(12));
    expect(h.states.at(-1)).toEqual([0, 11, 12]);
  });

  it("ignores duplicates and frames for channels it does not know", async () => {
    const h = setup();
    const s = await live(h);
    const before = h.states.length;
    s.push(frame(10));
    s.push({ ...frame(11), channel: "orderbook:0xother" });
    s.push("not json" as unknown as Record<string, unknown>);
    expect(h.states.length).toBe(before);
  });

  it("a gap triggers a REST re-snapshot and the stream continues from it", async () => {
    const h = setup();
    const s = await live(h);
    s.push(frame(11));
    h.snapshots.push({ data: [0, 11, 12, 13], epoch: E1, seq: 13 });
    s.push(frame(13)); // 12 was lost
    await flush();
    expect(h.loadSnapshot).toHaveBeenCalledTimes(2);
    expect(h.states.at(-1)).toEqual([0, 11, 12, 13]);
    s.push(frame(14));
    expect(h.states.at(-1)).toEqual([0, 11, 12, 13, 14]);
    expect(h.client.getStatus()).toBe("live");
  });

  it("a snapshot older than the buffered stream is fetched again", async () => {
    const h = setup();
    const s = await live(h);
    h.snapshots.push({ data: [0, 11], epoch: E1, seq: 11 }); // older than the gap
    h.snapshots.push({ data: [0, 11, 12, 13, 14], epoch: E1, seq: 14 });
    s.push(frame(15)); // gap (11..14 missing)
    await flush();
    expect(h.loadSnapshot).toHaveBeenCalledTimes(3);
    expect(h.states.at(-1)).toEqual([0, 11, 12, 13, 14, 15]); // second snapshot + buffered 15
  });

  it("a resync frame discards the buffer and re-snapshots", async () => {
    const h = setup();
    const s = await live(h);
    h.snapshots.push({ data: [7], epoch: E1, seq: 40 });
    s.push({ type: "resync", channel: CH, reason: "too_old", epoch: E1, seq: 40 });
    await flush();
    expect(h.states.at(-1)).toEqual([7]);
  });

  it("an epoch change on a live socket (Redis flush) re-snapshots instead of dropping seq 1 as a duplicate", async () => {
    const h = setup();
    const s = await live(h);
    s.push(frame(11));
    h.snapshots.push({ data: [100], epoch: E2, seq: 1 });
    s.push({ type: "resync", channel: CH, reason: "epoch_changed", epoch: E2, seq: 1 });
    s.push(frame(2, E2, 200));
    await flush();
    expect(h.states.at(-1)).toEqual([100, 200]);
    s.push(frame(3, E2, 300));
    expect(h.states.at(-1)).toEqual([100, 200, 300]);
  });

  it("an epoch change WITHOUT a resync frame is detected from the envelope alone", async () => {
    const h = setup();
    const s = await live(h);
    h.snapshots.push({ data: [100], epoch: E2, seq: 1 });
    s.push(frame(1, E2, 100));
    await flush();
    expect(h.loadSnapshot).toHaveBeenCalledTimes(2);
    expect(h.states.at(-1)).toEqual([100]);
  });
});

describe("reconnect and resume", () => {
  it("reconnects with backoff, resumes from (epoch, seq) and needs no snapshot when the log covers it", async () => {
    const h = setup();
    const s1 = await live(h);
    s1.push(frame(11));
    s1.drop(1006);
    expect(h.client.getStatus()).toBe("reconnecting");
    expect(h.sockets).toHaveLength(1);
    await h.clock.advance(1000);
    expect(h.sockets).toHaveLength(2);
    const s2 = h.last();
    s2.open();
    expect(s2.subscribes()[0]).toMatchObject({
      channels: [CH],
      resume: { [CH]: { epoch: E1, seq: 11 } },
    });
    s2.push(frame(12)); // replayed before `subscribed`
    s2.push(frame(13));
    s2.push({
      type: "subscribed",
      ref: "1",
      channels: [{ channel: CH, epoch: E1, seq: 13, replayed: 2 }],
    });
    await flush();
    expect(h.loadSnapshot).toHaveBeenCalledTimes(1); // only the first-ever snapshot
    expect(h.states.at(-1)).toEqual([0, 11, 12, 13]);
    expect(h.client.getStatus()).toBe("live");
  });

  it("falls back to a snapshot when the server answers resync", async () => {
    const h = setup();
    const s1 = await live(h);
    s1.drop(1001);
    await h.clock.advance(1000);
    const s2 = h.last();
    s2.open();
    h.snapshots.push({ data: [9], epoch: E1, seq: 99 });
    s2.push({ type: "resync", channel: CH, reason: "too_many", epoch: E1, seq: 99 });
    s2.push({
      type: "subscribed",
      ref: "1",
      channels: [{ channel: CH, epoch: E1, seq: 99, replayed: 0 }],
    });
    await flush();
    expect(h.states.at(-1)).toEqual([9]);
  });

  it("does not send resume for a channel that was still syncing when the socket dropped", async () => {
    const h = setup();
    h.client.subscribe(h.cfg);
    const s1 = h.last();
    s1.open();
    s1.drop(1006);
    await h.clock.advance(1000);
    const s2 = h.last();
    s2.open();
    expect(s2.subscribes()[0]).not.toHaveProperty("resume");
  });

  it("backoff grows 0.5s -> 30s, stays within jitter bounds, and resets after a good connection", async () => {
    const h = setup({ random: () => 1 }); // upper edge of the jitter band
    h.client.subscribe(h.cfg);
    const delays: number[] = [];
    for (let i = 0; i < 9; i++) {
      h.last().drop(1006);
      const [d] = h.clock.pending();
      delays.push(d as number);
      await h.clock.advance(d as number);
    }
    expect(delays).toEqual([500, 1000, 2000, 4000, 8000, 16000, 30000, 30000, 30000]);
    // a connection that completes the subscribe handshake resets the schedule
    h.snapshots.push({ data: [0], epoch: E1, seq: 1 });
    h.last().open();
    h.last().push({
      type: "subscribed",
      ref: "1",
      channels: [{ channel: CH, epoch: E1, seq: 1, replayed: 0 }],
    });
    await flush();
    h.last().drop(1006);
    expect(h.clock.pending()[0]).toBe(500);
  });

  it("backoffDelay stays inside [ceiling/2, ceiling] for any random value", () => {
    for (let attempt = 0; attempt < 12; attempt++) {
      const ceiling = Math.min(30_000, 500 * 2 ** attempt);
      for (const r of [0, 0.25, 0.5, 0.999, 1]) {
        const d = backoffDelay(attempt, 500, 30_000, r);
        expect(d).toBeGreaterThanOrEqual(ceiling / 2);
        expect(d).toBeLessThanOrEqual(ceiling);
      }
    }
  });

  it("4429 (rate limited) backs off hard instead of hammering", async () => {
    const h = setup({ random: () => 1 });
    const s = await live(h);
    s.drop(CLOSE.RATE_LIMITED);
    expect(h.clock.pending()[0]).toBeGreaterThanOrEqual(15_000);
  });

  it("start() after stop() reconnects and resumes", async () => {
    const h = setup();
    await live(h);
    h.client.stop();
    expect(h.client.getStatus()).toBe("idle");
    h.client.start();
    expect(h.sockets).toHaveLength(2);
    h.last().open();
    expect(h.last().subscribes()[0]).toMatchObject({ resume: { [CH]: { epoch: E1, seq: 10 } } });
  });
});

describe("close-code policy", () => {
  it("4401 ends the session: no reconnect storm, status session-ended; start() recovers", async () => {
    const h = setup();
    const s = await live(h);
    s.drop(CLOSE.SESSION_ENDED);
    expect(h.client.getStatus()).toBe("session-ended");
    expect(h.clock.pending()).toEqual([]);
    await h.clock.advance(120_000);
    expect(h.sockets).toHaveLength(1);
    h.client.start(); // after the user signed in again
    expect(h.sockets).toHaveLength(2);
    expect(h.client.getStatus()).toBe("connecting");
  });

  it.each([CLOSE.BAD_MESSAGE, CLOSE.TOO_LARGE])(
    "%d is a client bug: failed, not retried",
    async (code) => {
      const h = setup();
      const s = await live(h);
      s.drop(code);
      expect(h.client.getStatus()).toBe("failed");
      await h.clock.advance(120_000);
      expect(h.sockets).toHaveLength(1);
    },
  );

  it.each([CLOSE.GOING_AWAY, CLOSE.HEARTBEAT, CLOSE.SLOW_CONSUMER, 1006])(
    "%d reconnects",
    async (code) => {
      const h = setup();
      const s = await live(h);
      s.drop(code);
      await h.clock.advance(1000);
      expect(h.sockets).toHaveLength(2);
    },
  );
});

describe("heartbeat", () => {
  it("answers server pings with pong", async () => {
    const h = setup();
    const s = await live(h);
    s.push({ type: "ping", ts: 1 });
    expect(s.sent.at(-1)).toEqual({ op: "pong" });
  });

  it("drops and reconnects after two silent ping intervals; any inbound frame resets the clock", async () => {
    const h = setup();
    const s = await live(h);
    await h.clock.advance(39_000);
    s.push({ type: "ping", ts: 1 }); // alive: timer re-armed
    await h.clock.advance(39_000);
    expect(h.sockets).toHaveLength(1);
    await h.clock.advance(2_000); // 41 s of silence since the ping
    expect(s.closedWith).toBe(CLOSE.CLIENT_TIMEOUT);
    expect(h.client.getStatus()).toBe("reconnecting");
    await h.clock.advance(1000);
    expect(h.sockets).toHaveLength(2);
  });
});

describe("snapshot failures", () => {
  it("retries a failing snapshot with backoff and recovers", async () => {
    const h = setup();
    h.client.subscribe(h.cfg);
    const s = h.last();
    s.open();
    s.push({
      type: "subscribed",
      ref: "1",
      channels: [{ channel: CH, epoch: E1, seq: 5, replayed: 0 }],
    });
    await flush(); // first attempt throws: nothing queued
    expect(h.errors).toHaveLength(1);
    expect(h.client.getStatus()).toBe("resyncing");
    h.snapshots.push({ data: [1], epoch: E1, seq: 5 });
    await h.clock.advance(1000);
    expect(h.states.at(-1)).toEqual([1]);
    expect(h.client.getStatus()).toBe("live");
  });

  it("does not retry errors the app marks permanent (e.g. 401)", async () => {
    const h = setup({ shouldRetry: () => false });
    h.client.subscribe(h.cfg);
    const s = h.last();
    s.open();
    s.push({
      type: "subscribed",
      ref: "1",
      channels: [{ channel: CH, epoch: E1, seq: 5, replayed: 0 }],
    });
    await flush();
    await h.clock.advance(60_000);
    expect(h.loadSnapshot).toHaveBeenCalledTimes(1);
    expect(h.errors).toHaveLength(1);
  });

  it("surfaces server error frames for the channel", async () => {
    const h = setup();
    const s = await live(h);
    s.push({ type: "error", code: "unauthorized", message: "login required", channel: CH });
    expect(String(h.errors.at(-1))).toContain("unauthorized");
  });
});

describe("unsubscribe", () => {
  it("tells the server and stops delivering", async () => {
    const h = setup();
    h.snapshots.push({ data: [0], epoch: E1, seq: 10 });
    const sub = h.client.subscribe(h.cfg);
    const s = h.last();
    s.open();
    s.push({
      type: "subscribed",
      ref: "1",
      channels: [{ channel: CH, epoch: E1, seq: 10, replayed: 0 }],
    });
    await flush();
    sub.unsubscribe();
    expect(s.sent.at(-1)).toEqual({ op: "unsubscribe", channels: [CH] });
    const n = h.states.length;
    s.push(frame(11));
    expect(h.states.length).toBe(n);
  });

  it("subscribing while open sends a subscribe for just the new channel", async () => {
    const h = setup();
    const s = await live(h);
    h.client.subscribe({ ...h.cfg, name: "price:0xabc" });
    expect(s.subscribes().at(-1)).toMatchObject({ channels: ["price:0xabc"] });
  });
});

describe("types", () => {
  it("DataFrame shape is what the server sends", () => {
    const f: DataFrame = { type: "x", channel: "c", epoch: "e", seq: 1, ts: 1, data: null };
    expect(f.seq).toBe(1);
  });
});
