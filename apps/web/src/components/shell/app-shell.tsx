"use client";

import * as React from "react";
import Link from "next/link";
import { usePathname } from "next/navigation";
import { Menu, Zap } from "lucide-react";
import { ThemeToggle } from "@/components/theme-toggle";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogTrigger } from "@/components/ui/overlays";
import {
  SessionExpiredBanner,
  WalletButton,
  WrongNetworkDialog,
} from "@/components/wallet/wallet-ui";
import { cn } from "@/lib/utils";
import { ConnectionStatusIndicator, DisclosureBanner, NetworkBadge } from "./status-widgets";

/** Only routes that exist. 6b adds Trade, Meters, Insights, Activity, Settings and Admin as they ship. */
export const NAV_ITEMS: ReadonlyArray<{ href: string; label: string }> = [
  { href: "/", label: "Home" },
  { href: "/dashboard", label: "Dashboard" },
];

export function isActive(pathname: string, href: string): boolean {
  return href === "/" ? pathname === "/" : pathname === href || pathname.startsWith(`${href}/`);
}

function NavLinks({
  onNavigate,
  className,
}: {
  onNavigate?: () => void;
  className?: string;
}): React.JSX.Element {
  const pathname = usePathname();
  return (
    <nav aria-label="Main" className={className}>
      {NAV_ITEMS.map((item) => {
        const active = isActive(pathname, item.href);
        return (
          <Link
            key={item.href}
            href={item.href}
            onClick={onNavigate}
            aria-current={active ? "page" : undefined}
            className={cn(
              "inline-flex h-9 items-center rounded-lg px-3 text-sm font-medium transition-colors",
              active
                ? "bg-surface-3 text-foreground"
                : "text-muted-foreground hover:bg-surface-2 hover:text-foreground",
            )}
          >
            {item.label}
          </Link>
        );
      })}
    </nav>
  );
}

function MobileNav(): React.JSX.Element {
  const [open, setOpen] = React.useState(false);
  return (
    <Dialog open={open} onOpenChange={setOpen}>
      <DialogTrigger asChild>
        <Button variant="ghost" size="icon" className="md:hidden" aria-label="Open menu">
          <Menu aria-hidden />
        </Button>
      </DialogTrigger>
      <DialogContent title="Menu" side="left">
        <NavLinks onNavigate={() => setOpen(false)} className="flex flex-col gap-1" />
      </DialogContent>
    </Dialog>
  );
}

export function AppShell({ children }: { children: React.ReactNode }): React.JSX.Element {
  return (
    <div className="flex min-h-dvh flex-col">
      <a
        href="#main"
        className="bg-primary text-primary-foreground sr-only z-[60] rounded-lg px-3 py-2 text-sm font-medium focus:not-sr-only focus:fixed focus:top-2 focus:left-2"
      >
        Skip to content
      </a>
      <DisclosureBanner />
      <header className="bg-background/85 sticky top-0 z-40 border-b backdrop-blur">
        <div className="mx-auto flex h-14 max-w-7xl items-center gap-1 px-3 sm:gap-3 sm:px-6">
          <MobileNav />
          <Link
            href="/"
            className="mr-0.5 flex shrink-0 items-center gap-2 font-semibold tracking-tight"
            aria-label="WattSwap home"
          >
            <span className="bg-primary text-primary-foreground inline-flex size-7 items-center justify-center rounded-lg">
              <Zap aria-hidden className="size-4" />
            </span>
            <span className="hidden min-[480px]:inline">WattSwap</span>
          </Link>
          <NavLinks className="hidden items-center gap-1 md:flex" />
          <div className="ml-auto flex min-w-0 items-center gap-0.5 sm:gap-2">
            <ConnectionStatusIndicator />
            <NetworkBadge />
            <ThemeToggle />
            <WalletButton />
          </div>
        </div>
      </header>
      <SessionExpiredBanner />
      <main
        id="main"
        tabIndex={-1}
        className="mx-auto w-full max-w-7xl flex-1 px-4 py-6 outline-none sm:px-6 sm:py-8"
      >
        {children}
      </main>
      <footer className="text-muted-foreground border-t py-4 text-center text-xs">
        WattSwap testnet demo · simulated meters · mUSDC has no monetary value
      </footer>
      <WrongNetworkDialog />
    </div>
  );
}
