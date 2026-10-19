"use client";

/**
 * Component inventory: every component in every state, both themes (use the toggle), at any width.
 * This file is named page.dev.tsx on purpose: next.config.mjs only registers the `dev.tsx` page
 * extension when WEB_DEV_UI=1, so a normal (production) build does not contain this route at all.
 * All amounts, addresses and orders below are FIXTURES for visual states, not real data.
 */

import * as React from "react";
import { Plus } from "lucide-react";
import { toast } from "sonner";
import {
  EarningsCard,
  EnergyCard,
  PortfolioCard,
  PriceCard,
  type Load,
  type PortfolioData,
} from "@/components/dashboard/cards";
import { ThemeToggle } from "@/components/theme-toggle";
import { Address } from "@/components/ui/address";
import { Amount } from "@/components/ui/amount";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogTrigger,
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
  Tooltip,
} from "@/components/ui/overlays";
import {
  Alert,
  Badge,
  Card,
  CardBody,
  CardHeader,
  CardTitle,
  EmptyState,
  ErrorState,
  Input,
  Label,
  Segmented,
  Skeleton,
  Spinner,
  Stat,
} from "@/components/ui/primitives";
import { ConnectionStatusView, NetworkBadgeView } from "@/components/shell/status-widgets";
import {
  SignInGateView,
  WalletButtonView,
  WrongNetworkDialogView,
} from "@/components/wallet/wallet-ui";
import type { AuthFlow } from "@/lib/auth/hooks";
import type { AuthStep } from "@/lib/auth/flow";
import type { PortfolioTotals } from "@/lib/hooks/portfolio";
import type { PriceView } from "@/lib/hooks/market";
import type { ConnectionStatus } from "@/lib/realtime/client";

/* ------------------------------------------------------------------ fixtures */
const ADDR = "0xf39Fd6e51aad88F6F4ce6aB8827279cffFb92266";
const noop = (): void => undefined;
const flow = (step: AuthStep, extra: Partial<AuthFlow> = {}): AuthFlow => ({
  step,
  address: ADDR,
  error: undefined,
  pending: false,
  connect: noop,
  switchNetwork: noop,
  signIn: async () => undefined,
  signOut: async () => undefined,
  ...extra,
});

const TOTALS: PortfolioTotals = {
  period: "30d",
  asOf: "2026-10-19T12:00:00Z",
  producedWh: 184_250n,
  soldWh: 120_000n,
  boughtWh: 18_500n,
  grossSalesUsdc: 18_000_000n,
  feesUsdc: 180_000n,
  earnedUsdc: 17_820_000n,
  spentUsdc: 2_775_000n,
  confirmedTrades: 14,
  pendingTrades: 1,
  failedTrades: 1,
  pendingMints: 2,
  failedMints: 0,
};
const ZERO: PortfolioTotals = {
  ...TOTALS,
  producedWh: 0n,
  soldWh: 0n,
  boughtWh: 0n,
  grossSalesUsdc: 0n,
  feesUsdc: 0n,
  earnedUsdc: 0n,
  spentUsdc: 0n,
  confirmedTrades: 0,
  pendingTrades: 0,
  failedTrades: 0,
  pendingMints: 0,
};
const PRICE: PriceView = {
  price: 235_179n,
  bandBps: 500,
  breaker: "NORMAL",
  breakerReason: null,
  weatherStale: false,
  halted: false,
  ts: 1_792_401_331,
};
const PORTFOLIO: PortfolioData = {
  balances: { watt: 64_250n, usdc: 42_500_000n, wattAllowance: 1n, usdcAllowance: 0n },
  committed: { sellWh: 20_000n, buyUsdc: 3_000_000n, count: 3 },
  price: PRICE,
};
const boom = new Error("HTTP 503: the pricing service is restarting");
const err = (): Load<never> => ({ state: "error", error: boom, retry: noop });

const SWATCHES = [
  "background",
  "surface-1",
  "surface-2",
  "surface-3",
  "foreground",
  "muted-foreground",
  "primary",
  "accent",
  "success",
  "warning",
  "info",
  "destructive",
  "buy",
  "sell",
  "border",
  "border-strong",
  "ring",
  "chart-1",
  "chart-2",
  "chart-3",
  "chart-4",
  "chart-5",
] as const;
const TEXT_SIZES = ["xs", "sm", "base", "lg", "xl", "2xl", "3xl", "4xl"] as const;
const SIZE_CLASS: Record<(typeof TEXT_SIZES)[number], string> = {
  xs: "text-xs",
  sm: "text-sm",
  base: "text-base",
  lg: "text-lg",
  xl: "text-xl",
  "2xl": "text-2xl",
  "3xl": "text-3xl",
  "4xl": "text-4xl",
};
const STATUSES: ConnectionStatus[] = [
  "idle",
  "connecting",
  "live",
  "resyncing",
  "reconnecting",
  "session-ended",
  "failed",
];

function Section({
  id,
  title,
  children,
}: {
  id: string;
  title: string;
  children: React.ReactNode;
}): React.JSX.Element {
  return (
    <section id={id} aria-labelledby={`${id}-h`} className="scroll-mt-20 space-y-4">
      <h2 id={`${id}-h`} className="border-b pb-2 text-xl font-semibold">
        {title}
      </h2>
      {children}
    </section>
  );
}
function Cell({
  label,
  children,
}: {
  label: string;
  children: React.ReactNode;
}): React.JSX.Element {
  return (
    <div className="space-y-2">
      <p className="text-muted-foreground font-mono text-xs">{label}</p>
      {children}
    </div>
  );
}

export default function DevUi(): React.JSX.Element {
  const [seg, setSeg] = React.useState<"a" | "b" | "c">("b");
  const [wrongOpen, setWrongOpen] = React.useState(false);
  return (
    <div className="space-y-12" data-testid="dev-ui">
      <header className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <h1 className="text-3xl font-semibold tracking-tight">UI inventory</h1>
          <p className="text-muted-foreground text-sm">
            Every component in every state. Development builds only (WEB_DEV_UI=1). Values are
            fixtures.
          </p>
        </div>
        <ThemeToggle />
      </header>

      <Section id="color" title="Color tokens">
        <ul className="grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-6">
          {SWATCHES.map((t) => (
            <li key={t} className="space-y-1.5">
              <div className="h-12 rounded-md border" style={{ background: `var(--${t})` }} />
              <p className="font-mono text-xs">{t}</p>
            </li>
          ))}
        </ul>
        <div className="bg-surface-1 grid gap-2 rounded-xl border p-4 sm:grid-cols-3">
          {(
            [
              "foreground",
              "muted-foreground",
              "primary",
              "success",
              "warning",
              "info",
              "destructive",
              "buy",
              "sell",
            ] as const
          ).map((t) => (
            <p key={t} className="text-sm font-medium" style={{ color: `var(--${t})` }}>
              {t} text on surface-1
            </p>
          ))}
        </div>
      </Section>

      <Section id="type" title="Type scale">
        <div className="space-y-2">
          {TEXT_SIZES.map((s) => (
            <p key={s} className={SIZE_CLASS[s]}>
              <span className="text-muted-foreground mr-3 font-mono text-xs">{s}</span>
              Sunlight becomes tradable energy
            </p>
          ))}
          <p className="tabular font-mono text-lg">
            1,234.567 · 0.235179 · 10,000.000 (tabular figures)
          </p>
        </div>
      </Section>

      <Section id="space" title="Spacing, radius, elevation, motion">
        <div className="flex flex-wrap items-end gap-3">
          {[1, 2, 3, 4, 6, 8, 12, 16].map((n) => (
            <div key={n} className="space-y-1 text-center">
              <div
                className="bg-primary rounded-sm"
                style={{
                  width: `calc(var(--spacing) * ${n})`,
                  height: `calc(var(--spacing) * ${n})`,
                }}
              />
              <p className="font-mono text-xs">{n * 4}</p>
            </div>
          ))}
        </div>
        <div className="grid grid-cols-2 gap-4 sm:grid-cols-4">
          {(["rounded-sm", "rounded-md", "rounded-lg", "rounded-xl"] as const).map((r) => (
            <div
              key={r}
              className={`bg-surface-3 flex h-16 items-center justify-center border text-xs ${r}`}
            >
              {r}
            </div>
          ))}
        </div>
        <div className="grid grid-cols-2 gap-4 sm:grid-cols-4">
          {(["shadow-none", "shadow-e1", "shadow-e2", "shadow-e3"] as const).map((e) => (
            <div
              key={e}
              className={`bg-surface-1 flex h-16 items-center justify-center rounded-lg border text-xs ${e}`}
            >
              {e.replace("shadow-", "")}
            </div>
          ))}
        </div>
        <p className="text-muted-foreground text-sm">
          Motion: fast 120 ms, base 200 ms, slow 320 ms, easing standard/emphasized. Everything
          collapses to near-instant under prefers-reduced-motion. The skeleton shimmer below is the
          one looping animation.
        </p>
        <Skeleton className="h-6 w-48" />
      </Section>

      <Section id="buttons" title="Button">
        <div className="space-y-4">
          {(["default", "secondary", "outline", "ghost", "destructive", "link"] as const).map(
            (v) => (
              <div key={v} className="flex flex-wrap items-center gap-3">
                <span className="text-muted-foreground w-24 font-mono text-xs">{v}</span>
                <Button variant={v} size="sm">
                  Small
                </Button>
                <Button variant={v}>Default</Button>
                <Button variant={v} size="lg">
                  Large
                </Button>
                <Button variant={v} disabled>
                  Disabled
                </Button>
                <Button variant={v} loading>
                  Loading
                </Button>
              </div>
            ),
          )}
          <div className="flex flex-wrap items-center gap-3">
            <span className="text-muted-foreground w-24 font-mono text-xs">icon</span>
            <Button size="icon" aria-label="Add">
              <Plus aria-hidden />
            </Button>
            <Button size="icon-sm" variant="outline" aria-label="Add">
              <Plus aria-hidden />
            </Button>
            <Button>
              <Plus aria-hidden /> With icon
            </Button>
          </div>
        </div>
      </Section>

      <Section id="forms" title="Input, label, segmented">
        <div className="grid gap-4 sm:grid-cols-2">
          <Cell label="default">
            <div className="space-y-1.5">
              <Label htmlFor="i1">Amount (kWh)</Label>
              <Input id="i1" placeholder="0.000" inputMode="decimal" />
            </div>
          </Cell>
          <Cell label="filled">
            <div className="space-y-1.5">
              <Label htmlFor="i2">Limit price</Label>
              <Input id="i2" defaultValue="0.235179" />
            </div>
          </Cell>
          <Cell label="disabled">
            <div className="space-y-1.5">
              <Label htmlFor="i3">Locked</Label>
              <Input id="i3" defaultValue="1.000" disabled />
            </div>
          </Cell>
          <Cell label="invalid">
            <div className="space-y-1.5">
              <Label htmlFor="i4">Amount</Label>
              <Input id="i4" defaultValue="1.0001" aria-invalid aria-describedby="i4e" />
              <p id="i4e" className="text-destructive text-xs">
                WATT has 3 decimal places.
              </p>
            </div>
          </Cell>
        </div>
        <Segmented<"a" | "b" | "c">
          label="Demo segmented"
          value={seg}
          options={[
            { value: "a", label: "24h" },
            { value: "b", label: "7d" },
            { value: "c", label: "All" },
          ]}
          onChange={setSeg}
        />
      </Section>

      <Section id="feedback" title="Badge, alert, spinner, skeleton, toasts">
        <div className="flex flex-wrap gap-2">
          <Badge>neutral</Badge>
          <Badge tone="success">success</Badge>
          <Badge tone="warning">warning</Badge>
          <Badge tone="danger">danger</Badge>
          <Badge tone="info">info</Badge>
        </div>
        <div className="grid gap-3 sm:grid-cols-2">
          <Alert tone="info" title="Heads up">
            Informational message with body text.
          </Alert>
          <Alert tone="success" title="Done">
            Your order was placed.
          </Alert>
          <Alert tone="warning" title="Careful" action={<Button size="sm">Fix</Button>}>
            Something needs attention.
          </Alert>
          <Alert tone="danger" title="Failed">
            The request could not be completed.
          </Alert>
        </div>
        <div className="flex flex-wrap items-center gap-6">
          <Spinner label="Loading demo" /> <Skeleton className="h-8 w-40" />{" "}
          <Skeleton className="size-10 rounded-full" />
        </div>
        <div className="flex flex-wrap gap-2">
          <Button variant="outline" size="sm" onClick={() => toast.success("Signed in")}>
            Success toast
          </Button>
          <Button
            variant="outline"
            size="sm"
            onClick={() => toast.error("Signature declined in your wallet")}
          >
            Error toast
          </Button>
          <Button
            variant="outline"
            size="sm"
            onClick={() => toast("Order submitted", { description: "Waiting for the matcher." })}
          >
            Neutral toast
          </Button>
        </div>
      </Section>

      <Section id="data" title="Stat, amount, address">
        <div className="grid gap-6 sm:grid-cols-4">
          <Stat label="Default" value="1,234.567" hint="WATT" />
          <Stat label="Buy" value="0.2352" tone="buy" />
          <Stat label="Sell" value="0.2290" tone="sell" />
          <Stat label="Loading" value="" loading />
        </div>
        <div className="grid gap-3 sm:grid-cols-2">
          <Cell label="WATT">
            <Amount kind="watt" value={1_234_567n} />
          </Cell>
          <Cell label="mUSDC exact">
            <Amount kind="usdc" value={1_485_000n} />
          </Cell>
          <Cell label="mUSDC rounded (exact value in tooltip)">
            <Amount kind="usdc" value={1_485_001n} dp={2} />
          </Cell>
          <Cell label="price">
            <Amount kind="price" value={235_179n} />
          </Cell>
          <Cell label="signed positive">
            <Amount kind="usdc" value={17_820_000n} dp={2} signed />
          </Cell>
          <Cell label="beyond float precision">
            <Amount kind="usdc" value={123_456_789_012_345_678_901n} />
          </Cell>
        </div>
        <div className="flex flex-col gap-2">
          <Address value={ADDR} />
          <Address
            value={ADDR}
            href="https://sepolia.etherscan.io/address/0xf39Fd6e51aad88F6F4ce6aB8827279cffFb92266"
          />
          <Address value="0x6088a6b2e6c3f6d5e0f6a1d3f8f0d4b2c1a0e9f8d7c6b5a49382716059483726" />
        </div>
      </Section>

      <Section id="states" title="Empty and error states">
        <div className="grid gap-4 sm:grid-cols-2">
          <Card>
            <EmptyState
              title="No orders yet"
              action={
                <Button size="sm" variant="outline">
                  Place an order
                </Button>
              }
            >
              Orders you place appear here.
            </EmptyState>
          </Card>
          <Card>
            <ErrorState error={boom} onRetry={noop} />
          </Card>
          <Card>
            <CardHeader>
              <CardTitle>Card header</CardTitle>
              <Badge tone="info">info</Badge>
            </CardHeader>
            <CardBody>Card body content</CardBody>
          </Card>
        </div>
      </Section>

      <Section id="overlays" title="Dialog, menu, tooltip">
        <div className="flex flex-wrap gap-3">
          <Dialog>
            <DialogTrigger asChild>
              <Button variant="outline">Open dialog</Button>
            </DialogTrigger>
            <DialogContent
              title="Dialog title"
              description="Dialogs trap focus, close on Escape and restore focus."
            >
              <Button>Confirm</Button>
            </DialogContent>
          </Dialog>
          <DropdownMenu>
            <DropdownMenuTrigger asChild>
              <Button variant="outline">Open menu</Button>
            </DropdownMenuTrigger>
            <DropdownMenuContent>
              <DropdownMenuLabel>Account</DropdownMenuLabel>
              <DropdownMenuItem>Copy address</DropdownMenuItem>
              <DropdownMenuSeparator />
              <DropdownMenuItem>Sign out</DropdownMenuItem>
            </DropdownMenuContent>
          </DropdownMenu>
          <Tooltip content="Tooltips explain without hiding information">
            <Button variant="ghost">Hover or focus me</Button>
          </Tooltip>
        </div>
      </Section>

      <Section id="shell" title="Shell: network badge, connection status">
        <div className="flex flex-wrap items-center gap-3">
          <NetworkBadgeView chainKey="anvil" wrongNetwork={false} />
          <NetworkBadgeView chainKey="sepolia" wrongNetwork={false} />
          <NetworkBadgeView chainKey="anvil" wrongNetwork />
        </div>
        <div className="flex flex-wrap items-center gap-3">
          {STATUSES.map((s) => (
            <ConnectionStatusView key={s} status={s} />
          ))}
        </div>
      </Section>

      <Section id="wallet" title="Wallet: header button, sign-in gate, wrong network">
        <div className="flex flex-wrap items-center gap-3">
          {(["checking", "connect", "switch-network", "sign-in", "signing"] as const).map((s) => (
            <Cell key={s} label={s}>
              <WalletButtonView flow={flow(s)} expired={false} />
            </Cell>
          ))}
          <Cell label="sign-in (expired)">
            <WalletButtonView flow={flow("sign-in")} expired />
          </Cell>
          <Cell label="ready">
            <WalletButtonView flow={flow("ready")} expired={false} />
          </Cell>
        </div>
        <div className="grid gap-4 lg:grid-cols-2">
          {(["connect", "switch-network", "sign-in", "signing", "checking"] as const).map((s) => (
            <Cell key={s} label={`gate: ${s}`}>
              <SignInGateView flow={flow(s, s === "signing" ? { pending: true } : {})} />
            </Cell>
          ))}
          <Cell label="gate: sign-in with error">
            <SignInGateView
              flow={flow("sign-in", {
                error: "The signature request was declined in your wallet.",
              })}
            />
          </Cell>
        </div>
        <Alert
          tone="warning"
          title="Your session expired"
          action={<Button size="sm">Sign in again</Button>}
        >
          Sign in again to keep seeing your data and live updates. Nothing was lost.
        </Alert>
        <Button variant="outline" onClick={() => setWrongOpen(true)}>
          Open wrong-network dialog
        </Button>
        <WrongNetworkDialogView
          open={wrongOpen}
          onOpenChange={setWrongOpen}
          onSwitch={noop}
          pending={false}
          error={undefined}
        />
      </Section>

      <Section id="dash" title="Dashboard cards: ready, loading, empty, error">
        <div className="grid gap-4 lg:grid-cols-2">
          <Cell label="portfolio · ready">
            <PortfolioCard load={{ state: "ready", data: PORTFOLIO }} />
          </Cell>
          <Cell label="portfolio · empty wallet">
            <PortfolioCard
              load={{
                state: "ready",
                data: {
                  ...PORTFOLIO,
                  balances: { watt: 0n, usdc: 0n, wattAllowance: 0n, usdcAllowance: 0n },
                },
              }}
            />
          </Cell>
          <Cell label="portfolio · loading">
            <PortfolioCard load={{ state: "loading" }} />
          </Cell>
          <Cell label="portfolio · error">
            <PortfolioCard load={err()} />
          </Cell>
          <Cell label="energy · ready (with pending/failed)">
            <EnergyCard load={{ state: "ready", data: TOTALS }} />
          </Cell>
          <Cell label="energy · empty">
            <EnergyCard load={{ state: "ready", data: ZERO }} />
          </Cell>
          <Cell label="energy · loading">
            <EnergyCard load={{ state: "loading" }} />
          </Cell>
          <Cell label="energy · error">
            <EnergyCard load={err()} />
          </Cell>
          <Cell label="earnings · ready">
            <EarningsCard load={{ state: "ready", data: TOTALS }} />
          </Cell>
          <Cell label="earnings · empty">
            <EarningsCard load={{ state: "ready", data: ZERO }} />
          </Cell>
          <Cell label="earnings · loading">
            <EarningsCard load={{ state: "loading" }} />
          </Cell>
          <Cell label="earnings · error">
            <EarningsCard load={err()} />
          </Cell>
          <Cell label="price · ready">
            <PriceCard load={{ state: "ready", data: PRICE }} marketName="WATT-mUSDC" />
          </Cell>
          <Cell label="price · breaker tripped, stale weather">
            <PriceCard
              load={{
                state: "ready",
                data: { ...PRICE, breaker: "TRIPPED", weatherStale: true, halted: true },
              }}
              marketName="WATT-mUSDC"
            />
          </Cell>
          <Cell label="price · none yet">
            <PriceCard load={{ state: "ready", data: null }} />
          </Cell>
          <Cell label="price · loading">
            <PriceCard load={{ state: "loading" }} />
          </Cell>
        </div>
      </Section>
    </div>
  );
}
