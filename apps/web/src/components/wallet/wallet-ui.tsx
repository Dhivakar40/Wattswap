"use client";

import * as React from "react";
import { ChevronDown, LogOut, ShieldCheck } from "lucide-react";
import { Address } from "@/components/ui/address";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/overlays";
import { Alert, Card, CardBody, Spinner } from "@/components/ui/primitives";
import { type AuthFlow, useAuthFlow } from "@/lib/auth/hooks";
import { STEP_COPY, type AuthStep } from "@/lib/auth/flow";
import { useSessionStore } from "@/lib/auth/session";
import { explorerUrl, NETWORK_LABELS } from "@/lib/wagmi";
import { config } from "@/lib/config";
import { cn } from "@/lib/utils";

const ACTIONABLE = ["connect", "switch-network", "sign-in"] as const;
type Actionable = (typeof ACTIONABLE)[number];
const isActionable = (s: AuthStep): s is Actionable =>
  (ACTIONABLE as readonly string[]).includes(s);

function runStep(flow: AuthFlow, step: Actionable): void {
  if (step === "connect") flow.connect();
  else if (step === "switch-network") flow.switchNetwork();
  else void flow.signIn();
}

/* ------------------------------------------------------- Header wallet button */
export function WalletButtonView({
  flow,
  expired,
}: {
  flow: AuthFlow;
  expired: boolean;
}): React.JSX.Element {
  const { step } = flow;
  if (step === "checking" || step === "signing") {
    return (
      <Button
        variant="outline"
        disabled
        loading
        aria-label={step === "signing" ? "Waiting for signature" : "Loading account"}
      >
        {step === "signing" ? "Check wallet" : "Loading"}
      </Button>
    );
  }
  if (step === "ready") return <UserMenu flow={flow} />;
  const label = expired && step === "sign-in" ? "Sign in again" : STEP_COPY[step].cta;
  return (
    <Button
      onClick={() => runStep(flow, step)}
      variant={step === "switch-network" ? "outline" : "default"}
      className={step === "switch-network" ? "border-warning/60 text-warning" : undefined}
    >
      {label}
    </Button>
  );
}

export function WalletButton(): React.JSX.Element {
  const flow = useAuthFlow();
  const expired = useSessionStore((s) => s.session.status === "expired");
  return <WalletButtonView flow={flow} expired={expired} />;
}

function UserMenu({ flow }: { flow: AuthFlow }): React.JSX.Element {
  const user = useSessionStore((s) => s.session.user);
  const address = user?.address ?? flow.address ?? "";
  const net = NETWORK_LABELS[config.chainKey];
  const href = explorerUrl("address", address);
  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <Button variant="outline" aria-label="Account menu" data-testid="user-menu">
          <ShieldCheck aria-hidden className="text-success" />
          <span className="font-mono">
            {address.slice(0, 4)}…{address.slice(-4)}
          </span>
          <ChevronDown aria-hidden className="text-muted-foreground hidden sm:block" />
        </Button>
      </DropdownMenuTrigger>
      <DropdownMenuContent>
        <DropdownMenuLabel>
          Signed in on {net.name} ({net.detail}){user?.role === "admin" ? " · admin" : ""}
        </DropdownMenuLabel>
        <div className="px-2.5 pb-1.5">
          <Address value={address} href={href} />
        </div>
        <DropdownMenuSeparator />
        <DropdownMenuItem onSelect={() => void flow.signOut()}>
          <LogOut aria-hidden className="size-4" /> Sign out
        </DropdownMenuItem>
      </DropdownMenuContent>
    </DropdownMenu>
  );
}

/* ------------------------------------------------------- Wrong-network dialog */
export function WrongNetworkDialogView({
  open,
  onOpenChange,
  onSwitch,
  pending,
  error,
}: {
  open: boolean;
  onOpenChange(open: boolean): void;
  onSwitch(): void;
  pending: boolean;
  error: string | undefined;
}): React.JSX.Element {
  const net = NETWORK_LABELS[config.chainKey];
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent
        title="Wrong network"
        description={`WattSwap runs on ${net.name} (${net.detail}). Your wallet is connected to a different network, so nothing would work until you switch.`}
      >
        {error ? (
          <Alert tone="danger" className="mb-3" title="Could not switch">
            {error}
          </Alert>
        ) : null}
        <div className="flex flex-col-reverse gap-2 sm:flex-row sm:justify-end">
          <Button variant="ghost" onClick={() => onOpenChange(false)}>
            Not now
          </Button>
          <Button onClick={onSwitch} loading={pending}>
            Switch to {net.name}
          </Button>
        </div>
      </DialogContent>
    </Dialog>
  );
}

/** Opens by itself the moment a connected wallet is on the wrong chain; closes once it is fixed. */
export function WrongNetworkDialog(): React.JSX.Element {
  const flow = useAuthFlow();
  const wrong = flow.step === "switch-network";
  const [dismissed, setDismissed] = React.useState(false);
  React.useEffect(() => {
    if (!wrong) setDismissed(false); // re-arm for the next time
  }, [wrong]);
  return (
    <WrongNetworkDialogView
      open={wrong && !dismissed}
      onOpenChange={(o) => setDismissed(!o)}
      onSwitch={flow.switchNetwork}
      pending={flow.pending}
      error={flow.error}
    />
  );
}

/* --------------------------------------------------- Session expired banner */
export function SessionExpiredBanner(): React.JSX.Element | null {
  const expired = useSessionStore((s) => s.session.status === "expired");
  const flow = useAuthFlow();
  if (!expired) return null;
  return (
    <div className="mx-auto max-w-7xl px-4 pt-3 sm:px-6">
      <Alert
        tone="warning"
        title="Your session expired"
        action={
          flow.step === "sign-in" ? (
            <Button size="sm" onClick={() => void flow.signIn()}>
              Sign in again
            </Button>
          ) : isActionable(flow.step) ? (
            <Button size="sm" onClick={() => runStep(flow, flow.step as Actionable)}>
              {STEP_COPY[flow.step as Actionable].cta}
            </Button>
          ) : undefined
        }
      >
        Sign in again to keep seeing your data and live updates. Nothing was lost.
      </Alert>
    </div>
  );
}

/* ------------------------------------------------------------- Sign-in gate */
const STEPS: Array<{ id: Actionable; label: string }> = [
  { id: "connect", label: "Connect" },
  { id: "switch-network", label: "Network" },
  { id: "sign-in", label: "Sign in" },
];

export function SignInGateView({ flow }: { flow: AuthFlow }): React.JSX.Element {
  const { step } = flow;
  if (step === "checking") {
    return (
      <Card className="mx-auto max-w-lg">
        <CardBody className="flex items-center gap-3 py-8">
          <Spinner label="Restoring your session" />
          <p className="text-muted-foreground text-sm">Restoring your session…</p>
        </CardBody>
      </Card>
    );
  }
  const active = isActionable(step) ? step : "sign-in";
  const copy = STEP_COPY[active];
  const activeIndex = STEPS.findIndex((s) => s.id === active);
  return (
    <Card className="mx-auto max-w-lg">
      <CardBody className="space-y-5 p-5 sm:p-6">
        <ol className="flex items-center gap-1.5 sm:gap-2" aria-label="Sign-in progress">
          {STEPS.map((s, i) => (
            <li
              key={s.id}
              aria-current={i === activeIndex ? "step" : undefined}
              className={cn(
                "flex items-center gap-1.5 text-xs font-medium whitespace-nowrap",
                i === activeIndex
                  ? "text-foreground"
                  : i < activeIndex
                    ? "text-success"
                    : "text-muted-foreground",
              )}
            >
              <span
                className={cn(
                  "inline-flex size-5 items-center justify-center rounded-full border text-[0.6875rem]",
                  i === activeIndex && "border-primary bg-primary text-primary-foreground",
                  i < activeIndex && "border-success/50",
                )}
              >
                {i < activeIndex ? "✓" : i + 1}
              </span>
              {s.label}
              {i < STEPS.length - 1 ? (
                <span
                  aria-hidden
                  className="bg-border mx-0.5 h-px w-2 min-[400px]:mx-1 min-[400px]:w-4 sm:w-8"
                />
              ) : null}
            </li>
          ))}
        </ol>
        <div>
          <h2 className="text-xl font-semibold">{copy.title}</h2>
          <p className="text-muted-foreground mt-1 text-sm">{copy.body}</p>
        </div>
        {flow.error ? (
          <Alert tone="danger" title="That did not work">
            {flow.error}
          </Alert>
        ) : null}
        <Button
          size="lg"
          className="w-full"
          loading={flow.pending}
          onClick={() => runStep(flow, active)}
        >
          {step === "signing" ? "Waiting for your wallet…" : copy.cta}
        </Button>
      </CardBody>
    </Card>
  );
}

/** Renders `children` only for a signed-in user; otherwise walks them through connect, network and sign-in. */
export function SignInGate({ children }: { children: React.ReactNode }): React.JSX.Element {
  const flow = useAuthFlow();
  if (flow.step === "ready") return <>{children}</>;
  return <SignInGateView flow={flow} />;
}
