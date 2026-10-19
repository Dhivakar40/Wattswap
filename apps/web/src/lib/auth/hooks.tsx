"use client";

import * as React from "react";
import { useConnectModal } from "@rainbow-me/rainbowkit";
import { useQueryClient } from "@tanstack/react-query";
import { useAccount, useDisconnect, useSignMessage, useSwitchChain } from "wagmi";
import { onUnauthorized } from "@/lib/api/client";
import { config } from "@/lib/config";
import { useRealtimeClient } from "@/lib/realtime/provider";
import { deriveAuthStep, type AuthStep } from "./flow";
import { sameAddress, useSessionStore } from "./session";
import {
  fetchSessionUser,
  signInWithEthereum,
  SignInRejectedError,
  signOutFromApi,
} from "./sign-in";

/** How often a live session is re-checked against the API (also on focus). */
export const SESSION_RECHECK_MS = 60_000;

/**
 * Mounted once. Restores the session from the cookie, notices expiry (any 401, a failed re-check, or a
 * WebSocket close 4401) and ends the session when the wallet disconnects or switches account.
 */
export function SessionSync(): null {
  const status = useSessionStore((s) => s.session.status);
  const sessionAddress = useSessionStore((s) => s.session.user?.address);
  const { setAuthenticated, setSignedOut, expire } = useSessionStore.getState();
  const account = useAccount();
  const qc = useQueryClient();
  const realtime = useRealtimeClient();

  // restore
  React.useEffect(() => {
    let cancelled = false;
    fetchSessionUser()
      .then((user) => {
        if (cancelled) return;
        if (user) setAuthenticated(user);
        else setSignedOut();
      })
      .catch(() => {
        if (!cancelled) setSignedOut(); // API unreachable: pages show their own error states
      });
    return () => {
      cancelled = true;
    };
  }, [setAuthenticated, setSignedOut]);

  // expiry: any authenticated 401 or a re-check that comes back 401
  React.useEffect(() => onUnauthorized(() => expire()), [expire]);
  React.useEffect(() => {
    if (status !== "authenticated") return;
    const check = (): void => {
      fetchSessionUser()
        .then((user) => {
          if (user === null) expire();
        })
        .catch(() => {
          /* transient network error: not an expiry */
        });
    };
    const id = setInterval(check, SESSION_RECHECK_MS);
    window.addEventListener("focus", check);
    return () => {
      clearInterval(id);
      window.removeEventListener("focus", check);
    };
  }, [status, expire]);

  // an expired or ended session must not keep authenticated queries running
  React.useEffect(() => {
    if (status === "expired" || status === "signed-out") {
      void qc.cancelQueries({ queryKey: ["me"] });
      qc.removeQueries({ queryKey: ["me"] });
    }
  }, [status, qc]);

  // realtime: a 4401 close means the cookie is no longer valid
  React.useEffect(() => {
    if (realtime === null) return;
    return realtime.onStatus((s) => {
      if (s === "session-ended") expire();
    });
  }, [realtime, expire]);

  // wallet switched to another account while signed in: the session belongs to the old address
  const connectedAddress = account.status === "connected" ? account.address : undefined;
  React.useEffect(() => {
    if (
      status === "authenticated" &&
      connectedAddress &&
      !sameAddress(connectedAddress, sessionAddress)
    ) {
      void signOutFromApi().finally(() => {
        setSignedOut();
        qc.removeQueries({ queryKey: ["me"] });
      });
    }
  }, [connectedAddress, sessionAddress, status, setSignedOut, qc]);

  // the socket carries the cookie from its handshake: cycle it when the session changes
  const prevStatus = React.useRef(status);
  React.useEffect(() => {
    if (prevStatus.current !== status && realtime !== null) {
      if (status === "authenticated" || status === "signed-out") realtime.reconnect();
    }
    prevStatus.current = status;
  }, [status, realtime]);

  return null;
}

export interface AuthFlow {
  step: AuthStep;
  address: string | undefined;
  error: string | undefined;
  pending: boolean;
  connect(): void;
  switchNetwork(): void;
  signIn(): Promise<void>;
  signOut(): Promise<void>;
}

export function useAuthFlow(): AuthFlow {
  const session = useSessionStore((s) => s.session);
  const account = useAccount();
  const { openConnectModal } = useConnectModal();
  const { switchChainAsync, isPending: switching } = useSwitchChain();
  const { signMessageAsync } = useSignMessage();
  const { disconnectAsync } = useDisconnect();
  const qc = useQueryClient();
  const [error, setError] = React.useState<string | undefined>();

  const step = deriveAuthStep({
    session,
    wallet: { status: account.status, address: account.address, chainId: account.chainId },
    requiredChainId: config.chainId,
  });

  const signIn = React.useCallback(async (): Promise<void> => {
    const address = account.address;
    if (!address) return;
    setError(undefined);
    useSessionStore.getState().setSigning();
    try {
      const user = await signInWithEthereum({
        address,
        chainId: config.chainId,
        origin: window.location.origin,
        signMessage: (message) => signMessageAsync({ message }),
      });
      useSessionStore.getState().setAuthenticated(user);
    } catch (e) {
      // back to where we were: expired if it had expired, otherwise signed out
      const cur = useSessionStore.getState().session;
      if (cur.status === "signing" && cur.previous) {
        useSessionStore.setState({
          session: { status: "expired", user: null, previous: cur.previous },
        });
      } else {
        useSessionStore.getState().setSignedOut();
      }
      setError(
        e instanceof SignInRejectedError
          ? e.message
          : e instanceof Error
            ? e.message
            : "Sign-in failed",
      );
    }
  }, [account.address, signMessageAsync]);

  const signOut = React.useCallback(async (): Promise<void> => {
    setError(undefined);
    try {
      await signOutFromApi();
    } finally {
      useSessionStore.getState().setSignedOut();
      qc.removeQueries({ queryKey: ["me"] });
      await disconnectAsync().catch(() => undefined);
    }
  }, [disconnectAsync, qc]);

  return {
    step,
    address: account.address,
    error,
    pending: switching || step === "signing",
    connect: () => openConnectModal?.(),
    switchNetwork: () => {
      setError(undefined);
      switchChainAsync({ chainId: config.chainId }).catch((e: unknown) =>
        setError(e instanceof Error ? e.message : "Could not switch network"),
      );
    },
    signIn,
    signOut,
  };
}
