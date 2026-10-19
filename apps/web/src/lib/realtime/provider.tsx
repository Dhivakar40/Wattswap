"use client";

import * as React from "react";
import { config } from "@/lib/config";
import { useConnectionStore } from "@/lib/store/connection-store";
import { RealtimeClient } from "./client";

const Ctx = React.createContext<RealtimeClient | null>(null);

/** One RealtimeClient per tab. Connects lazily, on the first channel subscription. */
export function RealtimeProvider({
  children,
  client: injected,
}: {
  children: React.ReactNode;
  /** Tests inject a client wired to a fake socket. */
  client?: RealtimeClient;
}): React.JSX.Element {
  const setStatus = useConnectionStore((s) => s.setStatus);
  const [client] = React.useState(
    () =>
      injected ??
      (typeof window === "undefined"
        ? null
        : new RealtimeClient({ url: config.wsUrl, onStatus: setStatus })),
  );
  React.useEffect(() => {
    if (client === null) return;
    // Pause the socket while the tab is hidden for a long time? No: resume makes reconnects cheap and
    // the server drops idle clients, so we only make sure we are connected when the tab returns.
    const onVisible = (): void => {
      if (document.visibilityState === "visible" && client.getStatus() === "reconnecting") {
        client.reconnect();
      }
    };
    const onOnline = (): void => client.reconnect();
    document.addEventListener("visibilitychange", onVisible);
    window.addEventListener("online", onOnline);
    return () => {
      document.removeEventListener("visibilitychange", onVisible);
      window.removeEventListener("online", onOnline);
      client.stop();
    };
  }, [client]);
  return <Ctx.Provider value={client}>{children}</Ctx.Provider>;
}

export function useRealtimeClient(): RealtimeClient | null {
  return React.useContext(Ctx);
}
