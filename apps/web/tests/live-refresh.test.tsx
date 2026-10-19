import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, renderHook } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { RealtimeClient, type SocketLike } from "@/lib/realtime/client";
import { RealtimeProvider } from "@/lib/realtime/provider";

const ADDR = "0xf39fd6e51aad88f6f4ce6ab8827279cfffb92266";
const E = "epoch-u";

const get = vi.fn();
vi.mock("@/lib/api/client", async (orig) => {
  const real = await orig<typeof import("@/lib/api/client")>();
  return { ...real, api: { GET: (...args: unknown[]) => get(...args) } };
});

const { applyUserFrame, committedByOrders, LIVE_REFRESH_MS, toTotals, useUserStream } =
  await import("@/lib/hooks/portfolio");
type OrderOut = import("@/lib/hooks/portfolio").OrderOut;

class FakeSocket implements SocketLike {
  onopen: ((ev: unknown) => void) | null = null;
  onmessage: ((ev: { data: unknown }) => void) | null = null;
  onclose: ((ev: { code: number }) => void) | null = null;
  onerror: ((ev: unknown) => void) | null = null;
  sent: Array<Record<string, unknown>> = [];
  send(d: string): void {
    this.sent.push(JSON.parse(d) as Record<string, unknown>);
  }
  close(): void {}
  push(frame: Record<string, unknown>): void {
    this.onmessage?.({ data: JSON.stringify(frame) });
  }
}

const order = (hash: string, over: Partial<OrderOut> = {}): OrderOut => ({
  order_hash: hash,
  maker: ADDR,
  market_id: "0xmarket",
  side: "sell",
  amount_wh: "1000",
  limit_price: "500000",
  expiry: 2_000_000_000,
  nonce: "1",
  salt: "0x00",
  status: "open",
  filled_wh: "0",
  reserved_wh: "0",
  remaining_wh: "1000",
  cancel_reason: null,
  cancel_type: null,
  cancel_pending: false,
  created_at: "2026-10-19T12:00:00Z",
  updated_at: "2026-10-19T12:00:00Z",
  ...over,
});

const reply = (data: unknown, seq: number): { data: unknown; response: Response } => ({
  data,
  response: new Response("[]", {
    status: 200,
    headers: { "x-channel-epoch": E, "x-channel-seq": String(seq) },
  }),
});

const frame = (seq: number, type: "order" | "fill", data: unknown): Record<string, unknown> => ({
  type,
  channel: `user:${ADDR}`,
  epoch: E,
  seq,
  ts: 1,
  data,
});

describe("order stream reducer and totals", () => {
  it("keeps only live orders and removes ones that finished", () => {
    let book = applyUserFrame({}, {
      type: "order",
      channel: "c",
      epoch: E,
      seq: 1,
      ts: 1,
      data: order("0xa"),
    } as never);
    expect(Object.keys(book)).toEqual(["0xa"]);
    book = applyUserFrame(book, {
      type: "order",
      channel: "c",
      epoch: E,
      seq: 2,
      ts: 1,
      data: order("0xa", { status: "filled" }),
    } as never);
    expect(book).toEqual({});
    const same = applyUserFrame(book, {
      type: "fill",
      channel: "c",
      epoch: E,
      seq: 3,
      ts: 1,
      data: {},
    } as never);
    expect(same).toBe(book); // fills change totals, not the order map
  });

  it("is idempotent: the same order delta twice gives the same book", () => {
    const f = { type: "order", channel: "c", epoch: E, seq: 1, ts: 1, data: order("0xa") } as never;
    expect(applyUserFrame(applyUserFrame({}, f), f)).toEqual(applyUserFrame({}, f));
  });

  it("commits sell Wh and buy mUSDC exactly", () => {
    const book = {
      "0xa": order("0xa", { remaining_wh: "1500" }),
      "0xb": order("0xb", { side: "buy", remaining_wh: "2000", limit_price: "600000" }),
    };
    expect(committedByOrders(book)).toEqual({ sellWh: 1500n, buyUsdc: 1_200_000n, count: 2 });
  });

  it("converts API strings to bigint without float math", () => {
    const t = toTotals({
      period: "all",
      as_of: "x",
      since: null,
      produced_wh: "123456789012345678901",
      sold_wh: "1",
      bought_wh: "2",
      gross_sales_usdc: "100",
      fees_usdc: "1",
      earned_usdc: "99",
      spent_usdc: "5",
      confirmed_trades: 1,
      pending_trades: 0,
      failed_trades: 0,
      pending_mints: 0,
      failed_mints: 0,
    });
    expect(t.producedWh).toBe(123_456_789_012_345_678_901n);
    expect(t.earnedUsdc).toBe(99n);
  });
});

describe("live refresh on user:{address} (no polling)", () => {
  let socket: FakeSocket;
  let client: RealtimeClient;
  let qc: QueryClient;

  beforeEach(() => {
    vi.useFakeTimers();
    get.mockReset();
    get.mockImplementation(
      async (_path: string, opts: { params: { query: { status: string } } }) =>
        opts.params.query.status === "open" ? reply([order("0xa")], 10) : reply([], 11),
    );
    socket = new FakeSocket();
    client = new RealtimeClient({ url: "ws://t", createSocket: () => socket });
    qc = new QueryClient();
    vi.spyOn(qc, "invalidateQueries");
  });
  afterEach(() => {
    client.stop();
    vi.useRealTimers();
  });

  const wrapper = ({ children }: { children: ReactNode }): React.JSX.Element => (
    <QueryClientProvider client={qc}>
      <RealtimeProvider client={client}>{children}</RealtimeProvider>
    </QueryClientProvider>
  );

  async function connect(refetchBalances: () => void) {
    const hook = renderHook(() => useUserStream(ADDR, refetchBalances), { wrapper });
    await act(async () => {
      socket.onopen?.({});
      socket.push({
        type: "subscribed",
        ref: "1",
        channels: [{ channel: `user:${ADDR}`, epoch: E, seq: 10, replayed: 0 }],
      });
      await vi.advanceTimersByTimeAsync(0);
    });
    return hook;
  }

  it("subscribes to user:{address} and installs the open-orders snapshot", async () => {
    const hook = await connect(vi.fn());
    expect(socket.sent[0]).toMatchObject({ op: "subscribe", channels: [`user:${ADDR}`] });
    expect(Object.keys(hook.result.current.state ?? {})).toEqual(["0xa"]);
    expect(get).toHaveBeenCalledTimes(2); // open + partially_filled
  });

  it("a burst of events causes ONE debounced refresh of totals and balances", async () => {
    const refetchBalances = vi.fn();
    const hook = await connect(refetchBalances);
    expect(qc.invalidateQueries).not.toHaveBeenCalled(); // the first snapshot does not trigger a refresh

    await act(async () => {
      socket.push(frame(11, "order", order("0xb")));
      socket.push(frame(12, "fill", { id: "t1", status: "confirmed" }));
      socket.push(frame(13, "order", order("0xb", { status: "filled" })));
      await vi.advanceTimersByTimeAsync(LIVE_REFRESH_MS - 50);
    });
    expect(qc.invalidateQueries).not.toHaveBeenCalled(); // still inside the debounce window
    await act(async () => {
      await vi.advanceTimersByTimeAsync(LIVE_REFRESH_MS + 100); // the trailing edge, after the last event
    });
    expect(qc.invalidateQueries).toHaveBeenCalledTimes(1);
    expect(qc.invalidateQueries).toHaveBeenCalledWith({ queryKey: ["me", ADDR] });
    expect(refetchBalances).toHaveBeenCalledTimes(1);
    expect(Object.keys(hook.result.current.state ?? {})).toEqual(["0xa"]); // 0xb opened then filled
  });

  it("does not poll: silence means no refresh, however long", async () => {
    const refetchBalances = vi.fn();
    await connect(refetchBalances);
    await act(async () => {
      await vi.advanceTimersByTimeAsync(10 * 60_000);
    });
    expect(qc.invalidateQueries).not.toHaveBeenCalled();
    expect(refetchBalances).not.toHaveBeenCalled();
    expect(get).toHaveBeenCalledTimes(2);
  });

  it("a gap re-snapshots over REST and refreshes the figures that may have missed trades", async () => {
    const refetchBalances = vi.fn();
    await connect(refetchBalances);
    get.mockImplementation(async (_p: string, opts: { params: { query: { status: string } } }) =>
      opts.params.query.status === "open" ? reply([order("0xa"), order("0xc")], 20) : reply([], 20),
    );
    await act(async () => {
      socket.push(frame(15, "fill", { id: "t2" })); // seqs 11..14 were lost
      await vi.advanceTimersByTimeAsync(10);
    });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(LIVE_REFRESH_MS * 2); // snapshot installed, debounce settles
    });
    expect(get).toHaveBeenCalledTimes(4);
    expect(qc.invalidateQueries).toHaveBeenCalledTimes(1);
    expect(refetchBalances).toHaveBeenCalledTimes(1);
  });
});
