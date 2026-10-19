"use client";

import * as React from "react";
import { Alert, Segmented, type SegmentedOption } from "@/components/ui/primitives";
import { SignInGate } from "@/components/wallet/wallet-ui";
import { useSessionStore } from "@/lib/auth/session";
import { config } from "@/lib/config";
import { primaryMarket, type PriceView, useMarkets, useReferencePrice } from "@/lib/hooks/market";
import {
  committedByOrders,
  type Period,
  useBalances,
  usePortfolioTotals,
  useUserStream,
} from "@/lib/hooks/portfolio";
import {
  EarningsCard,
  EnergyCard,
  type Load,
  PortfolioCard,
  type PortfolioData,
  PriceCard,
} from "./cards";

const PERIODS: ReadonlyArray<SegmentedOption<Period>> = [
  { value: "24h", label: "24h" },
  { value: "7d", label: "7d" },
  { value: "30d", label: "30d" },
  { value: "all", label: "All" },
];

function toLoad<T>(q: {
  isPending: boolean;
  isError: boolean;
  error: unknown;
  data: T | undefined;
  refetch(): unknown;
}): Load<T> {
  if (q.isError) return { state: "error", error: q.error, retry: () => void q.refetch() };
  if (q.isPending || q.data === undefined) return { state: "loading" };
  return { state: "ready", data: q.data };
}

function DashboardContent({ address }: { address: string }): React.JSX.Element {
  const [period, setPeriod] = React.useState<Period>("all");
  const totals = usePortfolioTotals(address, period);
  const balances = useBalances(address);
  const markets = useMarkets();
  const market = primaryMarket(markets.data);
  const price = useReferencePrice(market?.market_id);
  const orders = useUserStream(address, () => void balances.refetch());

  const committed = orders.state ? committedByOrders(orders.state) : undefined;

  const portfolioLoad: Load<PortfolioData> = !balances.contractsConfigured
    ? {
        state: "error",
        error: `WattSwap contracts are not deployed on ${config.chainKey} in this build. Run make deploy-local, or set the right NEXT_PUBLIC_CHAIN_ID.`,
      }
    : balances.isError
      ? { state: "error", error: balances.error, retry: () => void balances.refetch() }
      : balances.isPending || balances.data === undefined
        ? { state: "loading" }
        : {
            state: "ready",
            data: { balances: balances.data, committed, price: price.state ?? undefined },
          };

  const priceLoad: Load<PriceView | null> = markets.isError
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

  return (
    <div className="space-y-5">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <h1 className="text-2xl font-semibold tracking-tight">Dashboard</h1>
          <p className="text-muted-foreground text-sm">
            Confirmed activity for your address. Updates live as trades settle.
          </p>
        </div>
        <Segmented<Period> label="Period" value={period} options={PERIODS} onChange={setPeriod} />
      </div>
      {orders.error && orders.state === null ? (
        <Alert tone="warning" title="Live updates are unavailable">
          Figures still load, but they will not refresh by themselves until the connection recovers.
        </Alert>
      ) : null}
      <div className="grid items-start gap-4 lg:grid-cols-2">
        <PortfolioCard load={portfolioLoad} />
        <PriceCard load={priceLoad} {...(market ? { marketName: market.name } : {})} />
        <EnergyCard load={toLoad(totals)} />
        <EarningsCard load={toLoad(totals)} />
      </div>
    </div>
  );
}

export function Dashboard(): React.JSX.Element {
  return (
    <SignInGate>
      <DashboardInner />
    </SignInGate>
  );
}

function DashboardInner(): React.JSX.Element | null {
  const address = useSessionStore((s) => s.session.user?.address);
  if (!address) return null;
  return <DashboardContent address={address} />;
}
