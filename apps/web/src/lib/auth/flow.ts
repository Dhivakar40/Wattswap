import type { SessionState } from "./session";
import { sameAddress } from "./session";

/** Where the user is on the way to a usable, signed-in session. */
export type AuthStep =
  | "checking" // restoring the session / wallet
  | "connect" // no wallet connected
  | "switch-network" // wallet is on a chain the app does not use
  | "sign-in" // wallet ready, no API session (or it expired)
  | "signing" // signature in flight
  | "ready";

export interface AuthInputs {
  session: SessionState;
  wallet: {
    status: "connected" | "connecting" | "reconnecting" | "disconnected";
    address: string | undefined;
    chainId: number | undefined;
  };
  requiredChainId: number;
}

/** Pure: the single step the UI should present next. */
export function deriveAuthStep({ session, wallet, requiredChainId }: AuthInputs): AuthStep {
  if (session.status === "unknown") return "checking";
  if (wallet.status === "connecting" || wallet.status === "reconnecting") return "checking";

  const walletConnected = wallet.status === "connected" && wallet.address !== undefined;
  if (session.status === "authenticated") {
    // A live session for a wallet that is not the one connected is not usable: sign in again.
    if (walletConnected && !sameAddress(wallet.address, session.user.address)) {
      return wallet.chainId === requiredChainId ? "sign-in" : "switch-network";
    }
    return "ready";
  }
  if (session.status === "signing") return "signing";
  if (!walletConnected) return "connect";
  if (wallet.chainId !== requiredChainId) return "switch-network";
  return "sign-in";
}

export const STEP_COPY: Record<
  Exclude<AuthStep, "checking" | "signing" | "ready">,
  { title: string; body: string; cta: string }
> = {
  connect: {
    title: "Connect your wallet",
    body: "WattSwap uses your wallet as your account. Nothing is sent until you approve it.",
    cta: "Connect wallet",
  },
  "switch-network": {
    title: "Switch network",
    body: "Your wallet is on a different network. WattSwap runs on a testnet, so switching is safe and free.",
    cta: "Switch network",
  },
  "sign-in": {
    title: "Sign in",
    body: "Sign a free message to prove you own this address. It does not send a transaction or cost gas.",
    cta: "Sign in with Ethereum",
  },
};
