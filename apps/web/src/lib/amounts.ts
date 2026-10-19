/**
 * The ONLY place that formats or parses WATT, mUSDC and price values.
 *
 * Everything is bigint in base units:
 *   - WATT: 3 decimals, 1 base unit = 1 Wh, so 1.000 WATT = 1000 Wh = 1 kWh (contracts/src/EnergyToken.sol)
 *   - mUSDC: 6 decimals
 *   - price: mUSDC base units per kWh (6 decimals)
 * No JS floats ever touch an amount: parsing is string -> bigint, formatting is bigint -> string.
 */

export const WATT_DECIMALS = 3;
export const USDC_DECIMALS = 6;
export const PRICE_DECIMALS = 6;
export const WH_PER_KWH = 1000n;
export const BPS_DENOMINATOR = 10_000n;

const GROUP = new Intl.NumberFormat("en-US", { useGrouping: true, maximumFractionDigits: 0 });

export type ParseResult =
  | { ok: true; value: bigint }
  | { ok: false; reason: "empty" | "invalid" | "too_many_decimals" | "negative" };

/** Parse a user-typed decimal string into base units. Strict: digits with one optional dot only. */
export function parseAmount(input: string, decimals: number): ParseResult {
  const s = input.trim();
  if (s === "") return { ok: false, reason: "empty" };
  if (s.startsWith("-")) return { ok: false, reason: "negative" };
  if (!/^(\d+\.?\d*|\.\d+)$/.test(s)) return { ok: false, reason: "invalid" };
  const [intPart = "", frac = ""] = s.split(".");
  if (frac.length > decimals) return { ok: false, reason: "too_many_decimals" };
  const digits = (intPart === "" ? "0" : intPart) + frac.padEnd(decimals, "0");
  return { ok: true, value: BigInt(digits) };
}

export const parseWatt = (s: string): ParseResult => parseAmount(s, WATT_DECIMALS);
export const parseUsdc = (s: string): ParseResult => parseAmount(s, USDC_DECIMALS);
export const parsePrice = (s: string): ParseResult => parseAmount(s, PRICE_DECIMALS);

/** Decimal string from the API (integer digits) to bigint; throws on anything else. */
export function toBigInt(value: string | number | bigint): bigint {
  if (typeof value === "bigint") return value;
  if (typeof value === "number") {
    if (!Number.isSafeInteger(value)) throw new RangeError(`not a safe integer: ${value}`);
    return BigInt(value);
  }
  if (!/^-?\d+$/.test(value)) throw new RangeError(`not an integer string: ${value}`);
  return BigInt(value);
}

export interface FormatOptions {
  /** Fraction digits to show. Default: all of them (exact). Fewer rounds half away from zero. */
  dp?: number;
  /** Drop trailing zeros beyond `minDp`. Default false. */
  trim?: boolean;
  minDp?: number;
  /** Group thousands with commas. Default true. */
  group?: boolean;
}

/** Exact bigint -> decimal string. Rounds (half away from zero) only when `dp` < decimals. */
export function formatFixed(value: bigint, decimals: number, opts: FormatOptions = {}): string {
  const { dp = decimals, trim = false, minDp = 0, group = true } = opts;
  if (dp < 0 || dp > decimals) throw new RangeError(`dp must be within 0..${decimals}`);
  const negative = value < 0n;
  let abs = negative ? -value : value;
  if (dp < decimals) {
    const drop = 10n ** BigInt(decimals - dp);
    const half = drop / 2n;
    abs = ((abs + half) / drop) * drop;
  }
  const scale = 10n ** BigInt(decimals);
  const whole = abs / scale;
  let frac = (abs % scale).toString().padStart(decimals, "0").slice(0, dp);
  if (trim) {
    frac = frac.replace(/0+$/, "");
    if (frac.length < minDp) frac = frac.padEnd(minDp, "0");
  }
  const wholeStr = group ? GROUP.format(whole) : whole.toString();
  const body = frac.length > 0 ? `${wholeStr}.${frac}` : wholeStr;
  return negative && abs !== 0n ? `-${body}` : body;
}

/** WATT / kWh amount (identical numbers: 1.000 WATT = 1 kWh). `1500n` -> "1.500". */
export const formatWatt = (wh: bigint, opts?: FormatOptions): string =>
  formatFixed(wh, WATT_DECIMALS, opts);
export const formatKwh = formatWatt;
/** mUSDC amount. `1485000n` -> "1.485000"; pass `{ dp: 2 }` for a compact figure. */
export const formatUsdc = (v: bigint, opts?: FormatOptions): string =>
  formatFixed(v, USDC_DECIMALS, opts);
/** Price in mUSDC per kWh. */
export const formatPrice = (v: bigint, opts?: FormatOptions): string =>
  formatFixed(v, PRICE_DECIMALS, opts);

/** cost = floor(fillWh * price / 1000), exactly the contract's rounding (docs/settlement.md). */
export function costOf(fillWh: bigint, price: bigint): bigint {
  return (fillWh * price) / WH_PER_KWH;
}

/** fee = floor(cost * feeBps / 10000). The seller receives cost - fee. */
export function feeOf(cost: bigint, feeBps: bigint): bigint {
  return (cost * feeBps) / BPS_DENOMINATOR;
}

/** Whole-number percentage (basis points, integer) -> "1.25%". */
export function formatBps(bps: bigint): string {
  return `${formatFixed(bps, 2, { trim: true, minDp: 0, group: false })}%`;
}
