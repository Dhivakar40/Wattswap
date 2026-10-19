"use client";

import * as React from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { energyTokenAbi, mockUSDCAbi } from "@wattswap/shared";
import { useReadContracts } from "wagmi";
import type { Address } from "viem";
import { toBigInt } from "@/lib/amounts";
import { api, channelPosition, unwrap } from "@/lib/api/client";
import type { components } from "@/lib/api/schema";
import { config } from "@/lib/config";
import type { DataFrame } from "@/lib/realtime/client";
import { useChannel, useDebouncedCallback } from "@/lib/realtime/hooks";

export type Period = components["schemas"]["PortfolioOut"]["period"];
export type OrderOut = components["schemas"]["OrderOut"];

/** Debounce window for live refreshes after a user-channel event. */
export const LIVE_REFRESH_MS = 400;

export interface PortfolioTotals {
  period: Period;
  asOf: string;
  producedWh: bigint;
  soldWh: bigint;
  boughtWh: bigint;
  grossSalesUsdc: bigint;
  feesUsdc: bigint;
  earnedUsdc: bigint;
  spentUsdc: bigint;
  confirmedTrades: number;
  pendingTrades: number;
  failedTrades: number;
  pendingMints: number;
  failedMints: number;
}

export function toTotals(p: components["schemas"]["PortfolioOut"]): PortfolioTotals {
  return {
    period: p.period,
    asOf: p.as_of,
    producedWh: toBigInt(p.produced_wh),
    soldWh: toBigInt(p.sold_wh),
    boughtWh: toBigInt(p.bought_wh),
    grossSalesUsdc: toBigInt(p.gross_sales_usdc),
    feesUsdc: toBigInt(p.fees_usdc),
    earnedUsdc: toBigInt(p.earned_usdc),
    spentUsdc: toBigInt(p.spent_usdc),
    confirmedTrades: p.confirmed_trades,
    pendingTrades: p.pending_trades,
    failedTrades: p.failed_trades,
    pendingMints: p.pending_mints,
    failedMints: p.failed_mints,
  };
}

/** Query keys for everything that belongs to one signed-in address (so sign-out can drop them all). */
export const meKey = (address: string, ...rest: unknown[]): unknown[] => [
  "me",
  address.toLowerCase(),
  ...rest,
];

export function usePortfolioTotals(address: string | undefined, period: Period) {
  return useQuery({
    queryKey: meKey(address ?? "none", "portfolio", period),
    enabled: address !== undefined,
    queryFn: async (): Promise<PortfolioTotals> =>
      toTotals(
        (await unwrap(api.GET("/api/v1/portfolio", { params: { query: { period } } }))).data,
      ),
  });
}

export interface Balances {
  watt: bigint;
  usdc: bigint;
  wattAllowance: bigint;
  usdcAllowance: bigint;
}

/** On-chain WATT / mUSDC balances and exchange allowances, read straight from the chain. */
export function useBalances(address: string | undefined) {
  const c = config.contracts;
  const owner = address as Address | undefined;
  const enabled = owner !== undefined && c !== undefined;
  const result = useReadContracts({
    allowFailure: false,
    query: { enabled },
    contracts:
      owner && c
        ? [
            {
              address: c.EnergyToken,
              abi: energyTokenAbi,
              functionName: "balanceOf",
              args: [owner],
            },
            { address: c.MockUSDC, abi: mockUSDCAbi, functionName: "balanceOf", args: [owner] },
            {
              address: c.EnergyToken,
              abi: energyTokenAbi,
              functionName: "allowance",
              args: [owner, c.WattSwapExchange],
            },
            {
              address: c.MockUSDC,
              abi: mockUSDCAbi,
              functionName: "allowance",
              args: [owner, c.WattSwapExchange],
            },
          ]
        : [],
  });
  const data: Balances | undefined = result.data
    ? {
        watt: result.data[0] as bigint,
        usdc: result.data[1] as bigint,
        wattAllowance: result.data[2] as bigint,
        usdcAllowance: result.data[3] as bigint,
      }
    : undefined;
  return {
    data,
    isPending: enabled && result.isPending,
    isError: result.isError,
    error: result.error,
    refetch: result.refetch,
    contractsConfigured: c !== undefined,
  };
}

export type OrderBook = Record<string, OrderOut>;

async function loadOpenOrders(): Promise<{ data: OrderBook; epoch: string; seq: number }> {
  // The head position is read by the server BEFORE each response's data, so the earlier of the two
  // is a safe lower bound: every later change has a larger seq, and deltas are idempotent upserts.
  const first = await unwrap(
    api.GET("/api/v1/orders", { params: { query: { status: "open", limit: 200 } } }),
  );
  const second = await unwrap(
    api.GET("/api/v1/orders", { params: { query: { status: "partially_filled", limit: 200 } } }),
  );
  const data: OrderBook = {};
  for (const o of [...first.data, ...second.data]) data[o.order_hash] = o;
  return { data, ...channelPosition(first.response) };
}

export function applyUserFrame(state: OrderBook, frame: DataFrame): OrderBook {
  if (frame.type !== "order") return state; // `fill` changes totals, not the order map
  const order = frame.data as OrderOut;
  const next = { ...state };
  if (order.status === "open" || order.status === "partially_filled")
    next[order.order_hash] = order;
  else delete next[order.order_hash];
  return next;
}

/** Wh and mUSDC (base units) currently committed to open orders. */
export function committedByOrders(book: OrderBook): {
  sellWh: bigint;
  buyUsdc: bigint;
  count: number;
} {
  let sellWh = 0n;
  let buyUsdc = 0n;
  const orders = Object.values(book);
  for (const o of orders) {
    const remaining = toBigInt(o.remaining_wh);
    if (o.side === "sell") sellWh += remaining;
    else buyUsdc += (remaining * toBigInt(o.limit_price)) / 1000n;
  }
  return { sellWh, buyUsdc, count: orders.length };
}

/**
 * The signed-in user's live stream: open orders as synced state, and a debounced refresh of every
 * figure that depends on trades (portfolio totals, balances) whenever anything arrives on
 * `user:{address}`. No polling.
 */
export function useUserStream(address: string | undefined, refetchBalances?: () => void) {
  const qc = useQueryClient();
  const refresh = useDebouncedCallback(() => {
    if (address) void qc.invalidateQueries({ queryKey: meKey(address) });
    refetchBalances?.();
  }, LIVE_REFRESH_MS);
  const channel = useChannel<OrderBook>({
    name: address ? `user:${address.toLowerCase()}` : null,
    loadSnapshot: loadOpenOrders,
    apply: applyUserFrame,
    onApplied: refresh,
  });
  // A (re-)installed snapshot after a gap, resync or reconnect may also have missed trades: refresh too,
  // but not for the very first snapshot (the page is loading those figures itself).
  const seen = React.useRef(false);
  React.useEffect(() => {
    if (channel.state === null) return;
    if (seen.current) refresh();
    seen.current = true;
  }, [channel.state, refresh]);
  return channel;
}
