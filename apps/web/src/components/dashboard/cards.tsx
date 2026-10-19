import * as React from "react";
import { Amount } from "@/components/ui/amount";
import {
  Badge,
  Card,
  CardBody,
  CardHeader,
  CardTitle,
  EmptyState,
  ErrorState,
  Skeleton,
  Stat,
} from "@/components/ui/primitives";
import { costOf, formatBps } from "@/lib/amounts";
import type { Balances, PortfolioTotals } from "@/lib/hooks/portfolio";
import type { PriceView } from "@/lib/hooks/market";

/** What a card needs to know about its data. Containers map react-query results into this. */
export type Load<T> =
  | { state: "loading" }
  | { state: "error"; error: unknown; retry?: () => void }
  | { state: "ready"; data: T };

function CardShell({
  title,
  aside,
  children,
  testId,
}: {
  title: string;
  aside?: React.ReactNode;
  children: React.ReactNode;
  testId: string;
}): React.JSX.Element {
  return (
    <Card aria-labelledby={`${testId}-title`} data-testid={testId}>
      <CardHeader>
        <CardTitle id={`${testId}-title`}>{title}</CardTitle>
        {aside}
      </CardHeader>
      <CardBody>{children}</CardBody>
    </Card>
  );
}

function Loading({ rows = 2 }: { rows?: number }): React.JSX.Element {
  return (
    <div className="grid grid-cols-2 gap-4" aria-busy="true">
      <span role="status" className="sr-only">
        Loading
      </span>
      {Array.from({ length: rows * 2 }, (_, i) => (
        <div key={i}>
          <Skeleton className="h-3 w-16" />
          <Skeleton className="mt-2 h-7 w-24" />
        </div>
      ))}
    </div>
  );
}

function Failed({
  load,
  title,
}: {
  load: Extract<Load<unknown>, { state: "error" }>;
  title: string;
}): React.JSX.Element {
  return (
    <ErrorState title={title} error={load.error} {...(load.retry ? { onRetry: load.retry } : {})} />
  );
}

/* ------------------------------------------------------------------ Portfolio */
export interface PortfolioData {
  balances: Balances;
  /** Wh and mUSDC committed to open orders; undefined until the live order stream has synced. */
  committed: { sellWh: bigint; buyUsdc: bigint; count: number } | undefined;
  price: PriceView | undefined;
}

export function PortfolioCard({ load }: { load: Load<PortfolioData> }): React.JSX.Element {
  return (
    <CardShell title="Portfolio" testId="card-portfolio">
      {load.state === "loading" ? <Loading /> : null}
      {load.state === "error" ? <Failed load={load} title="Could not read your balances" /> : null}
      {load.state === "ready" ? <PortfolioBody data={load.data} /> : null}
    </CardShell>
  );
}

function PortfolioBody({ data }: { data: PortfolioData }): React.JSX.Element {
  const { balances: b, committed, price } = data;
  const empty = b.watt === 0n && b.usdc === 0n;
  const value = price ? costOf(b.watt, price.price) : undefined;
  return (
    <div className="space-y-4">
      <div className="grid grid-cols-1 gap-4 min-[420px]:grid-cols-2">
        <Stat
          label="Energy tokens"
          value={<Amount kind="watt" value={b.watt} unit={false} />}
          hint="WATT · 1.000 = 1 kWh"
        />
        <Stat
          label="Mock USDC"
          value={<Amount kind="usdc" value={b.usdc} dp={2} unit={false} />}
          hint="mUSDC · testnet only"
        />
      </div>
      {empty ? (
        <EmptyState className="py-4" title="Nothing in this wallet yet">
          WATT appears after a simulated meter reading is minted. mUSDC comes from the testnet
          faucet.
        </EmptyState>
      ) : (
        <dl className="divide-y text-sm">
          <Row label="WATT at reference price">
            {value !== undefined ? (
              <Amount kind="usdc" value={value} dp={2} />
            ) : (
              <span className="text-muted-foreground">price unavailable</span>
            )}
          </Row>
          <Row label="In open sell orders">
            {committed ? (
              <Amount kind="watt" value={committed.sellWh} />
            ) : (
              <Skeleton className="h-4 w-16" />
            )}
          </Row>
          <Row label="Reserved by open buy orders">
            {committed ? (
              <Amount kind="usdc" value={committed.buyUsdc} dp={2} />
            ) : (
              <Skeleton className="h-4 w-16" />
            )}
          </Row>
          <Row label="Exchange approvals">
            <span className="flex gap-1.5">
              <Badge tone={b.wattAllowance > 0n ? "success" : "neutral"}>
                WATT {b.wattAllowance > 0n ? "approved" : "not approved"}
              </Badge>
              <Badge tone={b.usdcAllowance > 0n ? "success" : "neutral"}>
                mUSDC {b.usdcAllowance > 0n ? "approved" : "not approved"}
              </Badge>
            </span>
          </Row>
        </dl>
      )}
    </div>
  );
}

function Row({ label, children }: { label: string; children: React.ReactNode }): React.JSX.Element {
  return (
    <div className="flex min-h-10 flex-wrap items-center justify-between gap-x-3 gap-y-1 py-2">
      <dt className="text-muted-foreground">{label}</dt>
      <dd className="text-right">{children}</dd>
    </div>
  );
}

/* --------------------------------------------------------------------- Energy */
export function EnergyCard({ load }: { load: Load<PortfolioTotals> }): React.JSX.Element {
  return (
    <CardShell title="Energy" testId="card-energy">
      {load.state === "loading" ? <Loading /> : null}
      {load.state === "error" ? <Failed load={load} title="Could not load energy totals" /> : null}
      {load.state === "ready" ? <EnergyBody t={load.data} /> : null}
    </CardShell>
  );
}

function EnergyBody({ t }: { t: PortfolioTotals }): React.JSX.Element {
  const none = t.producedWh === 0n && t.soldWh === 0n && t.boughtWh === 0n;
  return (
    <div className="space-y-4">
      {none ? (
        <EmptyState className="py-4" title="No energy activity in this period">
          Register a simulated meter to produce energy, or place an order to trade it. Totals count
          confirmed activity only.
        </EmptyState>
      ) : (
        <div className="grid grid-cols-1 gap-4 min-[420px]:grid-cols-3">
          <Stat
            label="Produced"
            value={<Amount kind="watt" value={t.producedWh} unit={false} />}
            hint="kWh minted"
          />
          <Stat
            label="Sold"
            value={<Amount kind="watt" value={t.soldWh} unit={false} />}
            hint="kWh"
            tone="sell"
          />
          <Stat
            label="Bought"
            value={<Amount kind="watt" value={t.boughtWh} unit={false} />}
            hint="kWh"
            tone="buy"
          />
        </div>
      )}
      <PendingBadges t={t} />
    </div>
  );
}

function PendingBadges({ t }: { t: PortfolioTotals }): React.JSX.Element | null {
  const items: Array<{ n: number; label: string; tone: "warning" | "danger" }> = [
    { n: t.pendingMints, label: "mints pending", tone: "warning" },
    { n: t.pendingTrades, label: "trades settling", tone: "warning" },
    { n: t.failedMints, label: "mints failed", tone: "danger" },
    { n: t.failedTrades, label: "trades failed", tone: "danger" },
  ];
  const shown = items.filter((i) => i.n > 0);
  if (shown.length === 0) return null;
  return (
    <ul
      className="flex flex-wrap gap-1.5"
      aria-label="Activity that is not counted in the totals yet or failed"
    >
      {shown.map((i) => (
        <li key={i.label}>
          <Badge tone={i.tone}>
            {i.n} {i.label}
          </Badge>
        </li>
      ))}
    </ul>
  );
}

/* ------------------------------------------------------------------- Earnings */
export function EarningsCard({ load }: { load: Load<PortfolioTotals> }): React.JSX.Element {
  return (
    <CardShell title="Earnings" testId="card-earnings">
      {load.state === "loading" ? <Loading /> : null}
      {load.state === "error" ? <Failed load={load} title="Could not load earnings" /> : null}
      {load.state === "ready" ? <EarningsBody t={load.data} /> : null}
    </CardShell>
  );
}

function EarningsBody({ t }: { t: PortfolioTotals }): React.JSX.Element {
  if (t.grossSalesUsdc === 0n && t.spentUsdc === 0n) {
    return (
      <EmptyState className="py-4" title="No settled trades in this period">
        Earnings are the mUSDC you received from confirmed sales, after protocol fees.
      </EmptyState>
    );
  }
  return (
    <div className="space-y-4">
      <Stat
        label="Net earned"
        value={<Amount kind="usdc" value={t.earnedUsdc} dp={2} unit={false} />}
        hint="mUSDC received from sales after fees"
        tone={t.earnedUsdc > 0n ? "buy" : "default"}
      />
      <dl className="divide-y text-sm">
        <Row label="Gross sales">
          <Amount kind="usdc" value={t.grossSalesUsdc} dp={2} />
        </Row>
        <Row label="Protocol fees">
          <Amount kind="usdc" value={t.feesUsdc} dp={2} />
        </Row>
        <Row label="Spent on purchases">
          <Amount kind="usdc" value={t.spentUsdc} dp={2} />
        </Row>
      </dl>
    </div>
  );
}

/* ---------------------------------------------------------------- Market price */
export function PriceCard({
  load,
  marketName,
}: {
  load: Load<PriceView | null>;
  marketName?: string;
}): React.JSX.Element {
  return (
    <CardShell
      title={marketName ? `Reference price · ${marketName}` : "Reference price"}
      testId="card-price"
    >
      {load.state === "loading" ? <Loading rows={1} /> : null}
      {load.state === "error" ? <Failed load={load} title="Could not load the price" /> : null}
      {load.state === "ready" ? <PriceBody p={load.data} /> : null}
    </CardShell>
  );
}

function PriceBody({ p }: { p: PriceView | null }): React.JSX.Element {
  if (p === null) {
    return (
      <EmptyState className="py-4" title="No price yet">
        The pricing engine has not produced a tick for this market.
      </EmptyState>
    );
  }
  return (
    <div className="space-y-3">
      <Stat
        label="mUSDC per kWh"
        value={<Amount kind="price" value={p.price} dp={4} unit={false} />}
        hint={`Updated ${new Date(p.ts * 1000).toLocaleTimeString()}`}
      />
      <div className="flex flex-wrap gap-1.5">
        <Badge tone={p.breaker === "NORMAL" ? "success" : "warning"}>
          Breaker {p.breaker.toLowerCase()}
        </Badge>
        {p.weatherStale ? <Badge tone="warning">Weather data stale</Badge> : null}
        {p.halted ? <Badge tone="danger">Trading halted</Badge> : null}
        {p.bandBps !== null ? <Badge>±{formatBps(BigInt(p.bandBps))} band</Badge> : null}
      </div>
    </div>
  );
}
