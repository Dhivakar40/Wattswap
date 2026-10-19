/**
 * Session state machine. The API session is an httpOnly cookie the browser holds; this store only
 * mirrors what the server says about it.
 *
 *   unknown --/auth/me--> signed-out | authenticated
 *   signed-out --signIn--> signing --> authenticated
 *   authenticated --401 / WS 4401--> expired --signIn--> authenticated
 *   authenticated --wallet disconnect / account change / signOut--> signed-out
 */

import { create } from "zustand";
import type { components } from "@/lib/api/schema";

export type SessionUser = components["schemas"]["UserOut"];

export type SessionState =
  | { status: "unknown"; user: null }
  | { status: "signed-out"; user: null }
  | { status: "signing"; user: null; previous: SessionUser | null }
  | { status: "authenticated"; user: SessionUser }
  | { status: "expired"; user: null; previous: SessionUser };

interface SessionStore {
  session: SessionState;
  setUnknown(): void;
  setSignedOut(): void;
  setSigning(): void;
  setAuthenticated(user: SessionUser): void;
  /** Mark the session expired. A no-op unless the user was authenticated (a 401 while signed out is normal). */
  expire(): void;
}

export const useSessionStore = create<SessionStore>((set, get) => ({
  session: { status: "unknown", user: null },
  setUnknown: () => set({ session: { status: "unknown", user: null } }),
  setSignedOut: () => set({ session: { status: "signed-out", user: null } }),
  setSigning: () => {
    const cur = get().session;
    const previous = cur.status === "expired" ? cur.previous : cur.user;
    set({ session: { status: "signing", user: null, previous } });
  },
  setAuthenticated: (user) => set({ session: { status: "authenticated", user } }),
  expire: () => {
    const cur = get().session;
    if (cur.status === "authenticated") {
      set({ session: { status: "expired", user: null, previous: cur.user } });
    }
  },
}));

export function sameAddress(a: string | undefined | null, b: string | undefined | null): boolean {
  return a != null && b != null && a.toLowerCase() === b.toLowerCase();
}
