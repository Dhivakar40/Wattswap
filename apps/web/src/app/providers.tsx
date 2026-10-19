"use client";

import "@rainbow-me/rainbowkit/styles.css";
import * as React from "react";
import { darkTheme, lightTheme, RainbowKitProvider } from "@rainbow-me/rainbowkit";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { useTheme } from "next-themes";
import { Toaster } from "sonner";
import { WagmiProvider } from "wagmi";
import { ThemeProvider } from "@/components/theme-provider";
import { TooltipProvider } from "@/components/ui/overlays";
import { ApiError } from "@/lib/api/client";
import { SessionSync } from "@/lib/auth/hooks";
import { RealtimeProvider } from "@/lib/realtime/provider";
import { makeWagmiConfig } from "@/lib/wagmi";

export function makeQueryClient(): QueryClient {
  return new QueryClient({
    defaultOptions: {
      queries: {
        staleTime: 10_000,
        refetchOnWindowFocus: true,
        // Client errors (401/403/404/422) will not fix themselves; retry only transient failures.
        retry: (failures, error) =>
          failures < 2 && !(error instanceof ApiError && error.status >= 400 && error.status < 500),
      },
    },
  });
}

function ThemedWallet({ children }: { children: React.ReactNode }): React.JSX.Element {
  const { resolvedTheme } = useTheme();
  const options = {
    accentColor: "hsl(152 70% 45%)",
    accentColorForeground: "hsl(222 28% 7%)",
    borderRadius: "medium",
  } as const;
  return (
    <RainbowKitProvider
      modalSize="compact"
      theme={
        resolvedTheme === "light"
          ? lightTheme({
              ...options,
              accentColor: "hsl(152 75% 26%)",
              accentColorForeground: "white",
            })
          : darkTheme(options)
      }
    >
      {children}
    </RainbowKitProvider>
  );
}

function ThemedToaster(): React.JSX.Element {
  const { resolvedTheme } = useTheme();
  return (
    <Toaster
      theme={resolvedTheme === "light" ? "light" : "dark"}
      position="bottom-right"
      closeButton
      toastOptions={{
        classNames: { toast: "!bg-popover !text-popover-foreground !border-border !shadow-e2" },
      }}
    />
  );
}

export function Providers({ children }: { children: React.ReactNode }): React.JSX.Element {
  const [queryClient] = React.useState(makeQueryClient);
  const [wagmiConfig] = React.useState(makeWagmiConfig);
  return (
    <ThemeProvider>
      <WagmiProvider config={wagmiConfig}>
        <QueryClientProvider client={queryClient}>
          <ThemedWallet>
            <TooltipProvider>
              <RealtimeProvider>
                <SessionSync />
                {children}
                <ThemedToaster />
              </RealtimeProvider>
            </TooltipProvider>
          </ThemedWallet>
        </QueryClientProvider>
      </WagmiProvider>
    </ThemeProvider>
  );
}
