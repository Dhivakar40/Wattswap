import { describe, expect, it } from "vitest";
import { deriveAuthStep, type AuthInputs } from "@/lib/auth/flow";
import { useSessionStore, type SessionState } from "@/lib/auth/session";

const USER = {
  id: "u1",
  address: "0xf39Fd6e51aad88F6F4ce6aB8827279cffFb92266",
  role: "user" as const,
};
const REQUIRED = 31337;
const wallet = (over: Partial<AuthInputs["wallet"]> = {}): AuthInputs["wallet"] => ({
  status: "connected",
  address: USER.address,
  chainId: REQUIRED,
  ...over,
});
const step = (session: SessionState, w = wallet()): string =>
  deriveAuthStep({ session, wallet: w, requiredChainId: REQUIRED });
const disconnected = wallet({ status: "disconnected", address: undefined, chainId: undefined });

describe("deriveAuthStep", () => {
  const signedOut: SessionState = { status: "signed-out", user: null };

  it("waits while the session or wallet is being restored", () => {
    expect(step({ status: "unknown", user: null })).toBe("checking");
    expect(step(signedOut, wallet({ status: "reconnecting" }))).toBe("checking");
    expect(step(signedOut, wallet({ status: "connecting" }))).toBe("checking");
  });

  it("walks connect, then switch-network, then sign-in", () => {
    expect(step(signedOut, disconnected)).toBe("connect");
    expect(step(signedOut, wallet({ chainId: 1 }))).toBe("switch-network");
    expect(step(signedOut)).toBe("sign-in");
  });

  it("is ready only with a session for the connected wallet (address case ignored)", () => {
    expect(step({ status: "authenticated", user: USER })).toBe("ready");
    const lower = wallet({ address: USER.address.toLowerCase() });
    expect(step({ status: "authenticated", user: USER }, lower)).toBe("ready");
  });

  it("keeps a restored session usable while the wallet has not reconnected yet", () => {
    expect(step({ status: "authenticated", user: USER }, disconnected)).toBe("ready");
  });

  it("another account in the wallet means sign in again", () => {
    const other = wallet({ address: "0x70997970C51812dc3A010C7d01b50e0d17dc79C8" });
    expect(step({ status: "authenticated", user: USER }, other)).toBe("sign-in");
    expect(step({ status: "authenticated", user: USER }, { ...other, chainId: 1 })).toBe(
      "switch-network",
    );
  });

  it("an expired session asks for a new signature, not a reconnect", () => {
    expect(step({ status: "expired", user: null, previous: USER })).toBe("sign-in");
  });

  it("shows signing while the signature is pending", () => {
    expect(step({ status: "signing", user: null, previous: null })).toBe("signing");
  });
});

describe("session store", () => {
  it("expire() only affects an authenticated session", () => {
    const s = useSessionStore.getState();
    s.setSignedOut();
    s.expire();
    expect(useSessionStore.getState().session.status).toBe("signed-out"); // a 401 while signed out is normal
    s.setAuthenticated(USER);
    s.expire();
    const cur = useSessionStore.getState().session;
    expect(cur.status).toBe("expired");
    expect(cur.status === "expired" && cur.previous).toEqual(USER);
  });

  it("signing from expired remembers who was signed in", () => {
    const s = useSessionStore.getState();
    s.setAuthenticated(USER);
    s.expire();
    s.setSigning();
    const cur = useSessionStore.getState().session;
    expect(cur.status === "signing" && cur.previous).toEqual(USER);
  });
});
