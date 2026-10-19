"use client";

import * as React from "react";
import { PriceCard, type Load } from "@/components/dashboard/cards";
import { primaryMarket, type PriceView, useMarkets, useReferencePrice } from "@/lib/hooks/market";

/** Live reference price from the public API and `price:{market}` channel. No sign-in needed. */
export function LandingPrice(): React.JSX.Element {
  const markets = useMarkets();
  const market = primaryMarket(markets.data);
  const price = useReferencePrice(market?.market_id);

  const load: Load<PriceView | null> = markets.isError
    ? { state: "error", error: markets.error, retry: () => void markets.refetch() }
    : markets.isPending
      ? { state: "loading" }
      : market === undefined
        ? { state: "ready", data: null }
        : price.error && price.state === null
          ? { state: "error", error: price.error }
          : price.loading
            ? { state: "loading" }
            : { state: "ready", data: price.state };

  return <PriceCard load={load} {...(market ? { marketName: market.name } : {})} />;
}
