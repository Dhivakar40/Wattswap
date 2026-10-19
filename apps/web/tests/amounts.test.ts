import fc from "fast-check";
import { describe, expect, it } from "vitest";
import {
  costOf,
  feeOf,
  formatBps,
  formatFixed,
  formatPrice,
  formatUsdc,
  formatWatt,
  parseAmount,
  parsePrice,
  parseUsdc,
  parseWatt,
  toBigInt,
} from "@/lib/amounts";

describe("formatting", () => {
  it("formats WATT with exactly 3 dp", () => {
    expect(formatWatt(0n)).toBe("0.000");
    expect(formatWatt(1n)).toBe("0.001");
    expect(formatWatt(1000n)).toBe("1.000");
    expect(formatWatt(1_234_567n)).toBe("1,234.567");
  });

  it("formats mUSDC with exactly 6 dp and rounds half away from zero when asked", () => {
    expect(formatUsdc(1_485_000n)).toBe("1.485000");
    expect(formatUsdc(1_485_000n, { dp: 2 })).toBe("1.49");
    expect(formatUsdc(1_484_999n, { dp: 2 })).toBe("1.48");
    expect(formatUsdc(-1_485_000n, { dp: 2 })).toBe("-1.49");
    expect(formatUsdc(4_999n, { dp: 2 })).toBe("0.00");
  });

  it("never produces a negative zero", () => {
    expect(formatUsdc(-4_999n, { dp: 2 })).toBe("0.00");
  });

  it("trims trailing zeros on request", () => {
    expect(formatPrice(150_000n, { trim: true, minDp: 2 })).toBe("0.15");
    expect(formatPrice(1_000_000n, { trim: true })).toBe("1");
  });

  it("is exact beyond Number.MAX_SAFE_INTEGER", () => {
    const big = 123_456_789_012_345_678_901n;
    expect(formatUsdc(big)).toBe("123,456,789,012,345.678901");
  });

  it("rejects out-of-range dp", () => {
    expect(() => formatFixed(1n, 3, { dp: 4 })).toThrow(RangeError);
  });

  it("formats basis points", () => {
    expect(formatBps(100n)).toBe("1%");
    expect(formatBps(125n)).toBe("1.25%");
  });
});

describe("parsing", () => {
  it("parses well-formed decimals", () => {
    expect(parseWatt("1.5")).toEqual({ ok: true, value: 1500n });
    expect(parseWatt("0.001")).toEqual({ ok: true, value: 1n });
    expect(parseWatt(".5")).toEqual({ ok: true, value: 500n });
    expect(parseWatt("5.")).toEqual({ ok: true, value: 5000n });
    expect(parseUsdc(" 0.15 ")).toEqual({ ok: true, value: 150_000n });
    expect(parsePrice("0.235179")).toEqual({ ok: true, value: 235_179n });
  });

  it.each([
    ["", "empty"],
    ["  ", "empty"],
    ["-1", "negative"],
    ["1e3", "invalid"],
    ["1,5", "invalid"],
    ["0x10", "invalid"],
    ["NaN", "invalid"],
    ["1.2.3", "invalid"],
    ["abc", "invalid"],
    ["1.0001", "too_many_decimals"],
  ])("rejects %j", (input, reason) => {
    expect(parseWatt(input)).toEqual({ ok: false, reason });
  });

  it("keeps full precision for huge inputs", () => {
    expect(parseAmount("123456789012345678901.234567", 6)).toEqual({
      ok: true,
      value: 123_456_789_012_345_678_901_234_567n,
    });
  });

  it("round-trips: format(parse(s)) == s for canonical strings", () => {
    fc.assert(
      fc.property(
        fc.bigInt({ min: 0n, max: 10n ** 30n }),
        fc.integer({ min: 0, max: 8 }),
        (v, d) => {
          const s = formatFixed(v, d, { group: false });
          const parsed = parseAmount(s, d);
          expect(parsed).toEqual({ ok: true, value: v });
        },
      ),
    );
  });

  it("formatting is monotonic with ordering (same decimals, no grouping)", () => {
    fc.assert(
      fc.property(
        fc.bigInt({ min: 0n, max: 10n ** 24n }),
        fc.bigInt({ min: 0n, max: 10n ** 24n }),
        (a, b) => {
          const fa = parseAmount(formatFixed(a, 6, { group: false }), 6);
          const fb = parseAmount(formatFixed(b, 6, { group: false }), 6);
          expect(fa.ok && fb.ok && a < b === fa.value < fb.value).toBe(true);
        },
      ),
    );
  });
});

describe("settlement arithmetic (docs/settlement.md worked example)", () => {
  it("10 kWh at 0.15 with a 1% fee", () => {
    const cost = costOf(10_000n, 150_000n);
    expect(cost).toBe(1_500_000n);
    const fee = feeOf(cost, 100n);
    expect(fee).toBe(15_000n);
    expect(cost - fee).toBe(1_485_000n);
  });

  it("floors like the contract (never rounds up)", () => {
    expect(costOf(1n, 999n)).toBe(0n);
    expect(costOf(1_001n, 999n)).toBe(999n);
    expect(feeOf(99n, 100n)).toBe(0n);
  });

  it("fee never exceeds cost for fees up to 100%", () => {
    fc.assert(
      fc.property(
        fc.bigInt({ min: 0n, max: 10n ** 12n }),
        fc.bigInt({ min: 0n, max: 10_000n }),
        (c, bps) => {
          expect(feeOf(c, bps)).toBeLessThanOrEqual(c);
        },
      ),
    );
  });
});

describe("toBigInt", () => {
  it("accepts integer strings, safe integers and bigints", () => {
    expect(toBigInt("123456789012345678901234")).toBe(123_456_789_012_345_678_901_234n);
    expect(toBigInt(5)).toBe(5n);
    expect(toBigInt(7n)).toBe(7n);
  });
  it("rejects anything that is not an integer", () => {
    expect(() => toBigInt("1.5")).toThrow(RangeError);
    expect(() => toBigInt("")).toThrow(RangeError);
    expect(() => toBigInt(1.5)).toThrow(RangeError);
    expect(() => toBigInt(Number.MAX_SAFE_INTEGER + 2)).toThrow(RangeError);
  });
});
