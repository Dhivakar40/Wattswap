"use client";

import { useQuery } from "@tanstack/react-query";
import { api, channelPosition, unwrap } from "@/lib/api/client";
import { toBigInt } from "@/lib/amounts";
import type { DataFrame, Snapshot } from "@/lib/realtime/client";
import { useChannel } from "@/lib/realtime/hooks";
import type { components } from "@/lib/api/schema";

export type Market = components["schemas"]["MarketOut"];

export interface PriceView {
  /** mUSDC base units (6 dp) per kWh. */
  price: bigint;
  bandBps: number | null;
  breaker: string;
  breakerReason: string | null;
  weatherStale: boolean;
  halted: boolean;
  /** unix seconds of the tick */
  ts: number;
}

/** The price endpoint and channel carry a large open-ended body; read only what the UI uses, strictly. */
export function parsePriceView(data: unknown): PriceView {
  if (typeof data !== "object" || data === null) throw new TypeError("price body is not an object");
  const d = data as Record<string, unknown>;
  const price = d.price;
  if (typeof price !== "number" && typeof price !== "string")
    throw new TypeError("price body has no price");
  const ts = d.ts;
  if (typeof ts !== "number") throw new TypeError("price body has no ts");
  return {
    price: toBigInt(price),
    bandBps: typeof d.bandBps === "number" ? d.bandBps : null,
    breaker: typeof d.breaker === "string" ? d.breaker : "UNKNOWN",
    breakerReason: typeof d.breakerReason === "string" ? d.breakerReason : null,
    weatherStale: d.weatherStale === true,
    halted: d.halted === true,
    ts,
  };
}

export function useMarkets() {
  return useQuery({
    queryKey: ["markets"],
    queryFn: async (): Promise<Market[]> => (await unwrap(api.GET("/api/v1/markets"))).data,
    staleTime: 60_000,
  });
}

/** The market the dashboard values energy in: the primary enabled one, else the first enabled. */
export function primaryMarket(markets: Market[] | undefined): Market | undefined {
  return markets?.find((m) => m.is_primary && m.enabled) ?? markets?.find((m) => m.enabled);
}

async function loadPriceSnapshot(marketId: string): Promise<Snapshot<PriceView>> {
  const { data, response } = await unwrap(
    api.GET("/api/v1/markets/{market_id}/price", { params: { path: { market_id: marketId } } }),
  );
  const body = data as Record<string, unknown>;
  const epoch = typeof body.epoch === "string" ? body.epoch : null;
  const seq = typeof body.seq === "number" ? body.seq : null;
  const position = epoch !== null && seq !== null ? { epoch, seq } : channelPosition(response);
  return { data: parsePriceView(body), ...position };
}

/** Live reference price for a market: REST snapshot, then `price:{market}` deltas. */
export function useReferencePrice(marketId: string | undefined) {
  return useChannel<PriceView>({
    name: marketId ? `price:${marketId}` : null,
    loadSnapshot: () => loadPriceSnapshot(marketId ?? ""),
    apply: (_state: PriceView, frame: DataFrame) => parsePriceView(frame.data), // state-replacing
  });
}
