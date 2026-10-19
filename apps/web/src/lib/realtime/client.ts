/**
 * Browser realtime client for `/ws` (docs/api.md). One socket, many channels. Each channel runs the
 * snapshot+delta state machine in `sync.ts`; this class adds the transport: subscribe with `resume`,
 * REST snapshot loading, `resync` frames, application heartbeat, exponential backoff with jitter and the
 * close-code policy. Sockets, timers and randomness are injectable so everything is testable.
 */

import { ChannelSync, type Outcome } from "./sync";

// ------------------------------------------------------------------------------------------ wire
/** A data frame (the envelope). `data` is type-specific; amounts inside are decimal strings. */
export interface DataFrame {
  type: string;
  channel: string;
  epoch: string;
  seq: number;
  ts: number;
  data: unknown;
}

export interface SubscribedChannel {
  channel: string;
  epoch: string;
  seq: number;
  replayed: number;
}

export type ControlFrame =
  | { type: "subscribed"; ref?: string; channels: SubscribedChannel[] }
  | { type: "unsubscribed"; ref?: string; channels: string[] }
  | { type: "resync"; channel: string; reason: string; epoch: string; seq: number }
  | { type: "error"; code: string; message: string; ref?: string; channel?: string }
  | { type: "ping"; ts: number }
  | { type: "pong"; ref?: string };

const CONTROL_TYPES = new Set(["subscribed", "unsubscribed", "resync", "error", "ping", "pong"]);

export const CLOSE = {
  GOING_AWAY: 1001,
  BAD_MESSAGE: 4400,
  SESSION_ENDED: 4401,
  HEARTBEAT: 4408,
  SLOW_CONSUMER: 4409,
  TOO_LARGE: 4413,
  RATE_LIMITED: 4429,
  /** Client-side: no frame received within two ping intervals. */
  CLIENT_TIMEOUT: 4000,
} as const;

// -------------------------------------------------------------------------------------- injection
export interface SocketLike {
  send(data: string): void;
  close(code?: number, reason?: string): void;
  onopen: ((ev: unknown) => void) | null;
  onmessage: ((ev: { data: unknown }) => void) | null;
  onclose: ((ev: { code: number; reason?: string }) => void) | null;
  onerror: ((ev: unknown) => void) | null;
}

export interface Timers {
  setTimeout(fn: () => void, ms: number): unknown;
  clearTimeout(handle: unknown): void;
}

export type ConnectionStatus =
  | "idle" // not started / stopped
  | "connecting"
  | "live" // open and every channel in sync
  | "resyncing" // open, at least one channel re-fetching its snapshot
  | "reconnecting" // waiting to retry after a drop
  | "session-ended" // close 4401: sign in again
  | "failed"; // protocol bug (4400/4413): not retried

export interface RealtimeOptions {
  url: string;
  createSocket?: (url: string) => SocketLike;
  timers?: Timers;
  random?: () => number;
  baseDelayMs?: number;
  maxDelayMs?: number;
  /** The server pings every 20 s; the socket is dropped after two silent intervals. */
  pingIntervalMs?: number;
  onStatus?: (status: ConnectionStatus) => void;
}

// -------------------------------------------------------------------------------------- channels
export interface Snapshot<S> {
  data: S;
  epoch: string;
  seq: number;
}

export interface ChannelConfig<S> {
  name: string;
  /** GET the REST snapshot. MUST return the (epoch, seq) the server read BEFORE the data. */
  loadSnapshot(): Promise<Snapshot<S>>;
  /** Idempotent reducer for a data frame. */
  apply(state: S, frame: DataFrame): S;
  /** Called with the new state each time it changes (snapshot installed or delta applied). */
  onState(state: S): void;
  /** Called for each delta applied live (not for ones folded into a snapshot). */
  onApplied?(frame: DataFrame): void;
  onError?(error: unknown): void;
  /** Return false for errors that retrying cannot fix (e.g. 401). Default: always retry. */
  shouldRetry?(error: unknown): boolean;
}

export interface Subscription {
  unsubscribe(): void;
  /** Force a fresh snapshot (e.g. after the app learns the data is stale). */
  refresh(): void;
}

interface Entry {
  cfg: ChannelConfig<never>;
  sync: ChannelSync<unknown, DataFrame>;
  fetching: boolean;
  refetch: boolean;
  removed: boolean;
  sent: boolean; // included in a subscribe frame on the current socket
}

export function backoffDelay(attempt: number, base: number, max: number, random: number): number {
  const ceiling = Math.min(max, base * 2 ** attempt);
  return ceiling / 2 + random * (ceiling / 2); // "equal jitter": always in [ceiling/2, ceiling]
}

const defaultTimers: Timers = {
  setTimeout: (fn, ms) => setTimeout(fn, ms),
  clearTimeout: (h) => clearTimeout(h as ReturnType<typeof setTimeout>),
};

export class RealtimeClient {
  private readonly opts: Required<
    Pick<RealtimeOptions, "url" | "baseDelayMs" | "maxDelayMs" | "pingIntervalMs">
  >;
  private readonly createSocket: (url: string) => SocketLike;
  private readonly timers: Timers;
  private readonly random: () => number;
  private readonly onStatusCb: ((s: ConnectionStatus) => void) | undefined;

  private readonly channels = new Map<string, Entry>();
  private socket: SocketLike | null = null;
  private socketId = 0;
  private open = false;
  private status: ConnectionStatus = "idle";
  private wanted = false;
  private attempt = 0;
  private reconnectTimer: unknown = null;
  private livenessTimer: unknown = null;
  private refCounter = 0;
  private readonly listeners = new Set<(s: ConnectionStatus) => void>();

  constructor(options: RealtimeOptions) {
    this.opts = {
      url: options.url,
      baseDelayMs: options.baseDelayMs ?? 500,
      maxDelayMs: options.maxDelayMs ?? 30_000,
      pingIntervalMs: options.pingIntervalMs ?? 20_000,
    };
    this.createSocket =
      options.createSocket ?? ((url) => new WebSocket(url) as unknown as SocketLike);
    this.timers = options.timers ?? defaultTimers;
    this.random = options.random ?? Math.random;
    this.onStatusCb = options.onStatus;
  }

  // ----------------------------------------------------------------------------------- public API
  getStatus(): ConnectionStatus {
    return this.status;
  }

  onStatus(listener: (s: ConnectionStatus) => void): () => void {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  }

  /** Begin (or resume after `session-ended` / `failed`) connecting. Idempotent. */
  start(): void {
    this.wanted = true;
    if (this.socket !== null || this.reconnectTimer !== null) return;
    if (this.status === "idle" || this.status === "session-ended" || this.status === "failed") {
      this.attempt = 0;
    }
    this.connect();
  }

  /** Close for good. Channels stay registered; `start()` reconnects and resumes. */
  stop(): void {
    this.wanted = false;
    this.clearTimers();
    this.dropSocket(1000, "client stop");
    this.setStatus("idle");
  }

  /** Cycle the socket now (e.g. after sign-in so the handshake carries the new session cookie). */
  reconnect(): void {
    this.attempt = 0;
    this.clearTimers();
    this.dropSocket(1000, "reconnect");
    if (this.wanted) this.connect();
  }

  subscribe<S>(cfg: ChannelConfig<S>): Subscription {
    if (this.channels.has(cfg.name)) throw new Error(`already subscribed to ${cfg.name}`);
    const entry: Entry = {
      cfg: cfg as unknown as ChannelConfig<never>,
      sync: new ChannelSync<unknown, DataFrame>((state, frame) =>
        (cfg.apply as (s: unknown, f: DataFrame) => unknown)(state, frame),
      ),
      fetching: false,
      refetch: false,
      removed: false,
      sent: false,
    };
    this.channels.set(cfg.name, entry);
    if (this.open) this.sendSubscribe([entry]);
    this.start();
    return {
      unsubscribe: () => {
        if (entry.removed) return;
        entry.removed = true;
        this.channels.delete(cfg.name);
        if (this.open && entry.sent) this.send({ op: "unsubscribe", channels: [cfg.name] });
        this.refreshStatus();
      },
      refresh: () => {
        entry.sync.onResync();
        void this.runSnapshot(entry);
      },
    };
  }

  // ---------------------------------------------------------------------------------- connection
  private connect(): void {
    this.setStatus(this.attempt === 0 ? "connecting" : "reconnecting");
    const id = ++this.socketId;
    let sock: SocketLike;
    try {
      sock = this.createSocket(this.opts.url);
    } catch {
      this.scheduleReconnect();
      return;
    }
    this.socket = sock;
    sock.onopen = () => {
      if (id !== this.socketId) return;
      this.open = true;
      this.setStatus("connecting");
      this.armLiveness(id);
      const all = [...this.channels.values()];
      if (all.length > 0) this.sendSubscribe(all);
      else this.refreshStatus();
    };
    sock.onmessage = (ev) => {
      if (id !== this.socketId) return;
      this.armLiveness(id);
      this.handleRaw(ev.data);
    };
    sock.onerror = () => {
      /* the close event that follows carries the code */
    };
    sock.onclose = (ev) => {
      if (id !== this.socketId) return;
      this.handleClose(ev.code);
    };
  }

  private handleClose(code: number): void {
    this.open = false;
    this.socket = null;
    this.socketId += 1; // ignore stragglers from the dead socket
    this.clearLiveness();
    for (const e of this.channels.values()) e.sent = false;
    if (!this.wanted) {
      this.setStatus("idle");
      return;
    }
    if (code === CLOSE.SESSION_ENDED) {
      this.wanted = false;
      this.setStatus("session-ended");
      return;
    }
    if (code === CLOSE.BAD_MESSAGE || code === CLOSE.TOO_LARGE) {
      this.wanted = false;
      this.setStatus("failed");
      return;
    }
    if (code === CLOSE.RATE_LIMITED) this.attempt = Math.max(this.attempt, 6); // back off hard
    this.scheduleReconnect();
  }

  private scheduleReconnect(): void {
    const delay = backoffDelay(
      this.attempt,
      this.opts.baseDelayMs,
      this.opts.maxDelayMs,
      this.random(),
    );
    this.attempt += 1;
    this.setStatus("reconnecting");
    this.reconnectTimer = this.timers.setTimeout(() => {
      this.reconnectTimer = null;
      if (this.wanted) this.connect();
    }, delay);
  }

  private dropSocket(code: number, reason: string): void {
    const sock = this.socket;
    this.socket = null;
    this.open = false;
    this.socketId += 1;
    this.clearLiveness();
    for (const e of this.channels.values()) e.sent = false;
    if (sock !== null) {
      sock.onopen = sock.onmessage = sock.onclose = sock.onerror = null;
      try {
        sock.close(code, reason);
      } catch {
        /* already closed */
      }
    }
  }

  // ------------------------------------------------------------------------------------ heartbeat
  private armLiveness(id: number): void {
    this.clearLiveness();
    this.livenessTimer = this.timers.setTimeout(() => {
      if (id !== this.socketId) return;
      // Nothing for two ping intervals: the connection is dead even if the OS has not said so.
      this.dropSocket(CLOSE.CLIENT_TIMEOUT, "heartbeat timeout");
      if (this.wanted) this.scheduleReconnect();
    }, this.opts.pingIntervalMs * 2);
  }

  private clearLiveness(): void {
    if (this.livenessTimer !== null) this.timers.clearTimeout(this.livenessTimer);
    this.livenessTimer = null;
  }

  private clearTimers(): void {
    this.clearLiveness();
    if (this.reconnectTimer !== null) this.timers.clearTimeout(this.reconnectTimer);
    this.reconnectTimer = null;
  }

  // ----------------------------------------------------------------------------------- outbound
  private send(frame: Record<string, unknown>): void {
    if (this.socket !== null && this.open) this.socket.send(JSON.stringify(frame));
  }

  private sendSubscribe(entries: Entry[]): void {
    const resume: Record<string, { epoch: string; seq: number }> = {};
    for (const e of entries) {
      e.sent = true;
      const point = e.sync.resumePoint();
      if (point !== null) resume[e.cfg.name] = point;
    }
    this.send({
      op: "subscribe",
      channels: entries.map((e) => e.cfg.name),
      id: String(++this.refCounter),
      ...(Object.keys(resume).length > 0 ? { resume } : {}),
    });
  }

  // ------------------------------------------------------------------------------------ inbound
  private handleRaw(raw: unknown): void {
    if (typeof raw !== "string") return;
    let msg: unknown;
    try {
      msg = JSON.parse(raw);
    } catch {
      return;
    }
    if (typeof msg !== "object" || msg === null) return;
    const type = (msg as { type?: unknown }).type;
    if (typeof type !== "string") return;
    if (CONTROL_TYPES.has(type)) this.handleControl(msg as ControlFrame);
    else this.handleData(msg as DataFrame);
  }

  private handleControl(frame: ControlFrame): void {
    switch (frame.type) {
      case "ping":
        this.send({ op: "pong" });
        return;
      case "subscribed":
        this.attempt = 0; // a working connection resets the backoff
        for (const c of frame.channels) {
          const entry = this.channels.get(c.channel);
          if (entry !== undefined && entry.sync.syncing) void this.runSnapshot(entry);
        }
        this.refreshStatus();
        return;
      case "resync": {
        const entry = this.channels.get(frame.channel);
        if (entry === undefined) return;
        entry.sync.onResync();
        void this.runSnapshot(entry);
        return;
      }
      case "error": {
        if (frame.channel === undefined) return;
        const entry = this.channels.get(frame.channel);
        entry?.cfg.onError?.(new Error(`${frame.code}: ${frame.message}`));
        return;
      }
      default:
        return;
    }
  }

  private handleData(frame: DataFrame): void {
    const entry = this.channels.get(frame.channel);
    if (entry === undefined || typeof frame.seq !== "number" || typeof frame.epoch !== "string") {
      return;
    }
    const outcome: Outcome = entry.sync.onMessage(frame.epoch, frame.seq, frame);
    switch (outcome) {
      case "applied":
        (entry.cfg.onState as (s: unknown) => void)(entry.sync.state);
        entry.cfg.onApplied?.(frame as never);
        return;
      case "gap":
      case "epoch_changed":
        void this.runSnapshot(entry);
        return;
      default:
        return; // buffered or duplicate
    }
  }

  // ----------------------------------------------------------------------------------- snapshot
  private async runSnapshot(entry: Entry): Promise<void> {
    if (entry.fetching) {
      entry.refetch = true;
      return;
    }
    entry.fetching = true;
    this.refreshStatus();
    let attempt = 0;
    try {
      while (!entry.removed && this.open) {
        entry.refetch = false;
        let snap: Snapshot<unknown>;
        try {
          snap = await (entry.cfg.loadSnapshot as () => Promise<Snapshot<unknown>>)();
        } catch (error) {
          entry.cfg.onError?.(error);
          if (entry.cfg.shouldRetry?.(error) === false) return;
          await this.sleep(
            backoffDelay(attempt++, this.opts.baseDelayMs, this.opts.maxDelayMs, this.random()),
          );
          continue;
        }
        if (entry.removed) return;
        const outcome = entry.sync.loadSnapshot(snap.epoch, snap.seq, snap.data);
        if (outcome === "applied") {
          (entry.cfg.onState as (s: unknown) => void)(entry.sync.state);
          if (!entry.refetch) return;
        }
        // gap / epoch_changed: the snapshot was older than what we buffered (or predates a reset).
        // Fetch again right away; the server's next snapshot is newer.
      }
    } finally {
      entry.fetching = false;
      this.refreshStatus();
    }
  }

  private sleep(ms: number): Promise<void> {
    return new Promise((resolve) => this.timers.setTimeout(resolve, ms));
  }

  // -------------------------------------------------------------------------------------- status
  private refreshStatus(): void {
    if (!this.wanted || !this.open) return;
    const syncing = [...this.channels.values()].some((e) => e.sync.syncing);
    this.setStatus(syncing ? "resyncing" : "live");
  }

  private setStatus(next: ConnectionStatus): void {
    if (next === this.status) return;
    this.status = next;
    this.onStatusCb?.(next);
    for (const l of this.listeners) l(next);
  }
}
