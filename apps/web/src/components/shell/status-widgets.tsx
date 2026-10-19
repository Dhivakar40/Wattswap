"use client";

import * as React from "react";
import { Info } from "lucide-react";
import { useAccount } from "wagmi";
import { Tooltip } from "@/components/ui/overlays";
import { config } from "@/lib/config";
import type { ConnectionStatus } from "@/lib/realtime/client";
import { useConnectionStatus } from "@/lib/realtime/hooks";
import { NETWORK_LABELS } from "@/lib/wagmi";
import { cn } from "@/lib/utils";

/* -------------------------------------------------------- Network badge */
export function NetworkBadgeView({
  chainKey,
  wrongNetwork,
  className,
}: {
  chainKey: "anvil" | "sepolia";
  wrongNetwork: boolean;
  className?: string;
}): React.JSX.Element {
  const net = NETWORK_LABELS[chainKey];
  return (
    <span
      data-testid="network-badge"
      data-wrong-network={wrongNetwork || undefined}
      className={cn(
        "inline-flex h-7 items-center gap-1.5 rounded-full border px-2 text-xs font-medium whitespace-nowrap",
        wrongNetwork ? "border-warning/50 bg-warning/10 text-warning" : "bg-surface-2",
        className,
      )}
    >
      <span
        aria-hidden
        className={cn(
          "size-1.5 rounded-full",
          wrongNetwork ? "bg-warning" : chainKey === "anvil" ? "bg-info" : "bg-success",
        )}
      />
      {wrongNetwork ? (
        <>Wrong network</>
      ) : (
        <>
          {net.name}{" "}
          <span className="text-muted-foreground hidden font-normal sm:inline">· {net.detail}</span>
        </>
      )}
    </span>
  );
}

export function NetworkBadge(): React.JSX.Element {
  const { chainId, status } = useAccount();
  const wrong = status === "connected" && chainId !== undefined && chainId !== config.chainId;
  return <NetworkBadgeView chainKey={config.chainKey} wrongNetwork={wrong} />;
}

/* ---------------------------------------------------- Connection status */
const STATUS_VIEW: Record<ConnectionStatus, { label: string; dot: string; detail: string }> = {
  idle: {
    label: "Offline",
    dot: "bg-muted-foreground",
    detail: "Live updates are not connected yet.",
  },
  connecting: {
    label: "Connecting",
    dot: "bg-warning animate-pulse",
    detail: "Opening the live-updates connection.",
  },
  live: { label: "Live", dot: "bg-success", detail: "Receiving live updates." },
  resyncing: {
    label: "Syncing",
    dot: "bg-info animate-pulse",
    detail: "Catching up with the latest data.",
  },
  reconnecting: {
    label: "Reconnecting",
    dot: "bg-warning animate-pulse",
    detail: "Connection lost. Retrying automatically; nothing is lost.",
  },
  "session-ended": {
    label: "Signed out",
    dot: "bg-destructive",
    detail: "Your session ended. Sign in again to resume live updates.",
  },
  failed: {
    label: "Error",
    dot: "bg-destructive",
    detail: "The live connection hit a protocol error. Reload the page.",
  },
};

export function ConnectionStatusView({ status }: { status: ConnectionStatus }): React.JSX.Element {
  const v = STATUS_VIEW[status];
  return (
    <Tooltip content={v.detail}>
      <span
        role="status"
        aria-live="polite"
        data-testid="connection-status"
        data-status={status}
        className="text-muted-foreground inline-flex h-7 items-center gap-1.5 rounded-full px-1.5 text-xs font-medium"
      >
        <span aria-hidden className={cn("size-2 rounded-full", v.dot)} />
        <span className="sr-only min-[480px]:not-sr-only">
          <span className="sr-only">Live updates: </span>
          {v.label}
        </span>
      </span>
    </Tooltip>
  );
}

export function ConnectionStatusIndicator(): React.JSX.Element {
  return <ConnectionStatusView status={useConnectionStatus()} />;
}

/* ---------------------------------------------------- Demo disclosure */
export const DISCLOSURE_SHORT = "Demo mode · Testnet · Simulated smart meters";
export const DISCLOSURE_LONG =
  "WattSwap is a testnet demo. Smart meters are simulated by a labeled meter-simulator, payments use mUSDC, a mock stablecoin with no monetary value, and no physical energy is delivered or sold.";

/** Always visible; the detail can be expanded but the banner itself cannot be dismissed. */
export function DisclosureBanner(): React.JSX.Element {
  const [open, setOpen] = React.useState(false);
  return (
    <div
      role="region"
      aria-label="Demo disclosure"
      className="bg-warning/10 border-warning/30 text-foreground border-b"
    >
      <div className="mx-auto flex max-w-7xl items-center gap-2 px-4 py-1.5 text-xs sm:px-6">
        <Info aria-hidden className="text-warning size-3.5 shrink-0" />
        <p className="min-w-0 flex-1 font-medium text-balance" title={DISCLOSURE_LONG}>
          {DISCLOSURE_SHORT}
        </p>
        <button
          type="button"
          aria-expanded={open}
          aria-controls="disclosure-detail"
          onClick={() => setOpen((v) => !v)}
          className="text-warning shrink-0 rounded px-1 font-medium underline-offset-2 hover:underline"
        >
          {open ? "Hide" : "Details"}
        </button>
      </div>
      {open ? (
        <p
          id="disclosure-detail"
          className="text-muted-foreground mx-auto max-w-7xl px-4 pb-2 text-xs sm:px-6"
        >
          {DISCLOSURE_LONG}
        </p>
      ) : null}
    </div>
  );
}
