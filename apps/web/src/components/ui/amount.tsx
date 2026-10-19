import * as React from "react";
import { formatPrice, formatUsdc, formatWatt, type FormatOptions } from "@/lib/amounts";
import { cn } from "@/lib/utils";

type Kind = "watt" | "usdc" | "price";

const UNITS: Record<Kind, string> = { watt: "WATT", usdc: "mUSDC", price: "mUSDC/kWh" };
const FORMAT: Record<Kind, (v: bigint, o?: FormatOptions) => string> = {
  watt: formatWatt,
  usdc: formatUsdc,
  price: formatPrice,
};

/**
 * The one component that renders a token amount. Takes a bigint in base units, formats it through
 * `lib/amounts` (exact, tabular figures) and shows the unit. `dp` rounds for compact display; the
 * exact value is always in the `title` so nothing is silently hidden.
 */
export function Amount({
  kind,
  value,
  dp,
  unit = true,
  signed = false,
  className,
}: {
  kind: Kind;
  value: bigint;
  dp?: number;
  unit?: boolean;
  signed?: boolean;
  className?: string;
}): React.JSX.Element {
  const shown = FORMAT[kind](value, dp === undefined ? undefined : { dp });
  const exact = FORMAT[kind](value);
  const text = signed && value > 0n ? `+${shown}` : shown;
  return (
    <span
      className={cn("tabular font-mono", className)}
      title={dp === undefined ? undefined : `${exact} ${UNITS[kind]}`}
    >
      {text}
      {unit ? (
        <span className="text-muted-foreground ml-1 font-sans text-[0.75em]">{UNITS[kind]}</span>
      ) : null}
    </span>
  );
}
