import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import {
  EarningsCard,
  EnergyCard,
  PortfolioCard,
  PriceCard,
  type PortfolioData,
} from "@/components/dashboard/cards";
import {
  ConnectionStatusView,
  DISCLOSURE_LONG,
  DisclosureBanner,
  NetworkBadgeView,
} from "@/components/shell/status-widgets";
import { Amount } from "@/components/ui/amount";
import {
  SignInGateView,
  WalletButtonView,
  WrongNetworkDialogView,
} from "@/components/wallet/wallet-ui";
import { TooltipProvider } from "@/components/ui/overlays";
import type { AuthFlow } from "@/lib/auth/hooks";
import type { AuthStep } from "@/lib/auth/flow";
import type { PortfolioTotals } from "@/lib/hooks/portfolio";
import type { PriceView } from "@/lib/hooks/market";

const noop = (): void => undefined;
const flow = (step: AuthStep, over: Partial<AuthFlow> = {}): AuthFlow => ({
  step,
  address: "0xf39Fd6e51aad88F6F4ce6aB8827279cffFb92266",
  error: undefined,
  pending: false,
  connect: noop,
  switchNetwork: noop,
  signIn: async () => undefined,
  signOut: async () => undefined,
  ...over,
});

const TOTALS: PortfolioTotals = {
  period: "all",
  asOf: "2026-10-19T12:00:00Z",
  producedWh: 5_880n,
  soldWh: 2_500n,
  boughtWh: 0n,
  grossSalesUsdc: 760_000n,
  feesUsdc: 7_600n,
  earnedUsdc: 752_400n,
  spentUsdc: 0n,
  confirmedTrades: 5,
  pendingTrades: 1,
  failedTrades: 0,
  pendingMints: 0,
  failedMints: 2,
};
const ZERO: PortfolioTotals = {
  ...TOTALS,
  producedWh: 0n,
  soldWh: 0n,
  grossSalesUsdc: 0n,
  feesUsdc: 0n,
  earnedUsdc: 0n,
  pendingTrades: 0,
  failedMints: 0,
};
const PRICE: PriceView = {
  price: 597_200n,
  bandBps: 150,
  breaker: "NORMAL",
  breakerReason: null,
  weatherStale: false,
  halted: false,
  ts: 1_792_401_331,
};
const PORTFOLIO: PortfolioData = {
  balances: { watt: 3_380n, usdc: 752_400n, wattAllowance: 1n, usdcAllowance: 0n },
  committed: { sellWh: 0n, buyUsdc: 0n, count: 0 },
  price: PRICE,
};

describe("Amount", () => {
  it("renders exact bigint amounts with units", () => {
    render(<Amount kind="watt" value={1_234_567n} />);
    expect(screen.getByText("1,234.567")).toBeInTheDocument();
    expect(screen.getByText("WATT")).toBeInTheDocument();
  });
  it("keeps the exact value in the tooltip when rounded", () => {
    render(<Amount kind="usdc" value={1_485_001n} dp={2} />);
    expect(screen.getByText("1.49")).toHaveAttribute("title", "1.485001 mUSDC");
  });
});

describe("dashboard cards", () => {
  it("portfolio: shows balances, value at reference price and approvals", () => {
    render(<PortfolioCard load={{ state: "ready", data: PORTFOLIO }} />);
    const card = screen.getByTestId("card-portfolio");
    expect(within(card).getByText("3.380")).toBeInTheDocument();
    expect(within(card).getByText("0.75")).toBeInTheDocument();
    expect(within(card).getByText("2.02")).toBeInTheDocument(); // 3.380 kWh * 0.5972
    expect(within(card).getByText("WATT approved")).toBeInTheDocument();
    expect(within(card).getByText("mUSDC not approved")).toBeInTheDocument();
  });

  it("portfolio: explains an empty wallet instead of showing zeros alone", () => {
    const empty = {
      ...PORTFOLIO,
      balances: { watt: 0n, usdc: 0n, wattAllowance: 0n, usdcAllowance: 0n },
    };
    render(<PortfolioCard load={{ state: "ready", data: empty }} />);
    expect(screen.getByText("Nothing in this wallet yet")).toBeInTheDocument();
  });

  it("every card has a loading state that is announced and marked busy", () => {
    render(
      <>
        <PortfolioCard load={{ state: "loading" }} />
        <EnergyCard load={{ state: "loading" }} />
        <EarningsCard load={{ state: "loading" }} />
        <PriceCard load={{ state: "loading" }} />
      </>,
    );
    expect(screen.getAllByRole("status")).toHaveLength(4);
    expect(document.querySelectorAll('[aria-busy="true"]')).toHaveLength(4);
  });

  it("every card has an error state with retry", async () => {
    const retry = vi.fn();
    render(<EnergyCard load={{ state: "error", error: new Error("HTTP 503"), retry }} />);
    expect(screen.getByRole("alert")).toHaveTextContent("Could not load energy totals");
    expect(screen.getByRole("alert")).toHaveTextContent("HTTP 503");
    await userEvent.click(screen.getByRole("button", { name: "Try again" }));
    expect(retry).toHaveBeenCalledTimes(1);
  });

  it("energy: totals, plus pending and failed counts that are not in the totals", () => {
    render(<EnergyCard load={{ state: "ready", data: TOTALS }} />);
    expect(screen.getByText("5.880")).toBeInTheDocument();
    expect(screen.getByText("2.500")).toBeInTheDocument();
    expect(screen.getByText("2 mints failed")).toBeInTheDocument();
    expect(screen.getByText("1 trades settling")).toBeInTheDocument();
  });

  it("energy and earnings: empty states explain what counts", () => {
    render(
      <>
        <EnergyCard load={{ state: "ready", data: ZERO }} />
        <EarningsCard load={{ state: "ready", data: ZERO }} />
      </>,
    );
    expect(screen.getByText("No energy activity in this period")).toBeInTheDocument();
    expect(screen.getByText("No settled trades in this period")).toBeInTheDocument();
  });

  it("earnings: net = gross minus fees, exactly", () => {
    render(<EarningsCard load={{ state: "ready", data: TOTALS }} />);
    const card = screen.getByTestId("card-earnings");
    expect(within(card).getByText("0.75")).toBeInTheDocument(); // 752_400 -> 0.75
    expect(within(card).getByText("0.76")).toBeInTheDocument(); // gross 760_000
    expect(within(card).getByText("0.01")).toBeInTheDocument(); // fees 7_600
  });

  it("price: shows breaker, stale-weather and halted flags", () => {
    render(
      <PriceCard
        load={{
          state: "ready",
          data: { ...PRICE, breaker: "TRIPPED", weatherStale: true, halted: true },
        }}
      />,
    );
    expect(screen.getByText("0.5972")).toBeInTheDocument();
    expect(screen.getByText("Breaker tripped")).toBeInTheDocument();
    expect(screen.getByText("Weather data stale")).toBeInTheDocument();
    expect(screen.getByText("Trading halted")).toBeInTheDocument();
    expect(screen.getByText("±1.5% band")).toBeInTheDocument();
  });

  it("price: empty state when the engine has not ticked", () => {
    render(<PriceCard load={{ state: "ready", data: null }} />);
    expect(screen.getByText("No price yet")).toBeInTheDocument();
  });
});

describe("shell widgets", () => {
  it("network badge names the network and flags the wrong one", () => {
    const { rerender } = render(<NetworkBadgeView chainKey="anvil" wrongNetwork={false} />);
    expect(screen.getByTestId("network-badge")).toHaveTextContent("Anvil");
    rerender(<NetworkBadgeView chainKey="sepolia" wrongNetwork={false} />);
    expect(screen.getByTestId("network-badge")).toHaveTextContent("Sepolia");
    rerender(<NetworkBadgeView chainKey="anvil" wrongNetwork />);
    expect(screen.getByTestId("network-badge")).toHaveTextContent("Wrong network");
    expect(screen.getByTestId("network-badge")).toHaveAttribute("data-wrong-network", "true");
  });

  it("connection status renders every state with an accessible name", () => {
    const states = [
      "idle",
      "connecting",
      "live",
      "resyncing",
      "reconnecting",
      "session-ended",
      "failed",
    ] as const;
    for (const s of states) {
      const { unmount } = render(
        <TooltipProvider>
          <ConnectionStatusView status={s} />
        </TooltipProvider>,
      );
      expect(screen.getByTestId("connection-status")).toHaveAttribute("data-status", s);
      expect(screen.getByRole("status")).toHaveTextContent("Live updates:");
      unmount();
    }
  });

  it("the disclosure is always visible, expands, and cannot be dismissed", async () => {
    render(<DisclosureBanner />);
    expect(screen.getByText(/Demo mode/)).toBeVisible();
    expect(screen.queryByText(DISCLOSURE_LONG)).toBeNull();
    const toggle = screen.getByRole("button", { name: "Details" });
    expect(toggle).toHaveAttribute("aria-expanded", "false");
    await userEvent.click(toggle);
    expect(screen.getByText(DISCLOSURE_LONG)).toBeVisible();
    expect(screen.getByText(DISCLOSURE_LONG).textContent).toMatch(/no physical energy/i);
    expect(screen.queryByRole("button", { name: /dismiss|close|hide banner/i })).toBeNull();
    await userEvent.click(screen.getByRole("button", { name: "Hide" }));
    expect(screen.getByText(/Demo mode/)).toBeVisible(); // collapse only hides the detail
  });
});

describe("wallet UI", () => {
  it.each([
    ["connect", /connect wallet/i],
    ["switch-network", /switch network/i],
    ["sign-in", /sign in with ethereum/i],
  ] as const)("gate at %s offers exactly the next action", async (step, label) => {
    const connect = vi.fn();
    const switchNetwork = vi.fn();
    const signIn = vi.fn(async () => undefined);
    render(<SignInGateView flow={flow(step, { connect, switchNetwork, signIn })} />);
    expect(screen.getByRole("list", { name: "Sign-in progress" })).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: label }));
    const calls = { connect, "switch-network": switchNetwork, "sign-in": signIn };
    expect(calls[step]).toHaveBeenCalledTimes(1);
    const others = Object.entries(calls).filter(([k]) => k !== step);
    for (const [, fn] of others) expect(fn).not.toHaveBeenCalled();
  });

  it("gate marks progress and shows sign-in errors without hiding the button", () => {
    render(
      <SignInGateView
        flow={flow("sign-in", { error: "The signature request was declined in your wallet." })}
      />,
    );
    expect(
      screen.getByText("The signature request was declined in your wallet."),
    ).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /sign in with ethereum/i })).toBeEnabled();
    expect(screen.getByText("Sign in", { selector: "li" }).closest("li")).toHaveAttribute(
      "aria-current",
      "step",
    );
  });

  it("header button: busy while checking, session menu when ready, re-sign copy when expired", () => {
    const { rerender } = render(<WalletButtonView flow={flow("checking")} expired={false} />);
    expect(screen.getByRole("button")).toBeDisabled();
    rerender(<WalletButtonView flow={flow("sign-in")} expired />);
    expect(screen.getByRole("button", { name: "Sign in again" })).toBeInTheDocument();
    rerender(<WalletButtonView flow={flow("ready")} expired={false} />);
    expect(screen.getByRole("button", { name: "Account menu" })).toHaveTextContent("0xf3…2266");
  });

  it("wrong-network dialog switches with one click and can be dismissed", async () => {
    const onSwitch = vi.fn();
    const onOpenChange = vi.fn();
    render(
      <WrongNetworkDialogView
        open
        onOpenChange={onOpenChange}
        onSwitch={onSwitch}
        pending={false}
        error={undefined}
      />,
    );
    const dialog = screen.getByRole("dialog", { name: "Wrong network" });
    await userEvent.click(within(dialog).getByRole("button", { name: /switch to anvil/i }));
    expect(onSwitch).toHaveBeenCalledTimes(1);
    await userEvent.click(within(dialog).getByRole("button", { name: "Not now" }));
    expect(onOpenChange).toHaveBeenCalledWith(false);
  });

  it("wrong-network dialog surfaces a failed switch", () => {
    render(
      <WrongNetworkDialogView
        open
        onOpenChange={noop}
        onSwitch={noop}
        pending={false}
        error="User rejected the request."
      />,
    );
    expect(screen.getByRole("alert")).toHaveTextContent("User rejected the request.");
  });
});
