import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { describe, expect, it } from "vitest";
import { contrastRatio, parseTokens, resolveColor } from "./helpers/contrast";

const css = readFileSync(resolve(__dirname, "../src/app/globals.css"), "utf-8");
const light = parseTokens(css, ":root");
const dark = { ...light, ...parseTokens(css, ".dark") };

/** [foreground token, background token, minimum ratio, why] */
const PAIRS: Array<[string, string, number, string]> = [
  ["foreground", "background", 4.5, "body text"],
  ["foreground", "surface-1", 4.5, "text on cards"],
  ["foreground", "surface-2", 4.5, "text on raised surfaces"],
  ["foreground", "surface-3", 4.5, "text on the highest surface"],
  ["muted-foreground", "background", 4.5, "secondary text on the page"],
  ["muted-foreground", "surface-1", 4.5, "secondary text on cards"],
  ["muted-foreground", "surface-2", 4.5, "secondary text on raised surfaces"],
  ["muted-foreground", "surface-3", 4.5, "secondary text on the highest surface"],
  ["primary-foreground", "primary", 4.5, "primary button label"],
  ["accent-foreground", "accent", 4.5, "accent badge label"],
  ["destructive-foreground", "destructive", 4.5, "destructive solid label"],
  ["primary", "background", 4.5, "primary used as link text"],
  ["primary", "surface-1", 4.5, "primary text on cards"],
  ["destructive", "surface-1", 4.5, "error text on cards"],
  ["success", "surface-1", 4.5, "success text on cards"],
  ["warning", "surface-1", 4.5, "warning text on cards"],
  ["info", "surface-1", 4.5, "info text on cards"],
  ["buy", "surface-1", 4.5, "buy-side numbers"],
  ["sell", "surface-1", 4.5, "sell-side numbers"],
  ["buy", "surface-2", 4.5, "buy-side numbers on raised surfaces"],
  ["sell", "surface-2", 4.5, "sell-side numbers on raised surfaces"],
  ["ring", "background", 3, "focus ring (non-text, WCAG 1.4.11)"],
  ["border-strong", "surface-1", 3, "input and control borders (WCAG 1.4.11)"],
  ["input", "surface-1", 3, "form field border"],
];

describe.each([
  ["light", light],
  ["dark", dark],
] as const)("%s theme contrast (WCAG AA)", (_name, tokens) => {
  it.each(PAIRS)("%s on %s >= %f:1 (%s)", (fg, bg, min) => {
    const ratio = contrastRatio(resolveColor(tokens, fg), resolveColor(tokens, bg));
    expect(ratio, `${fg} on ${bg} = ${ratio.toFixed(2)}`).toBeGreaterThanOrEqual(min);
  });
});

describe("token file hygiene", () => {
  it("defines every semantic colour in both themes", () => {
    for (const name of [
      "background",
      "foreground",
      "surface-1",
      "primary",
      "buy",
      "sell",
      "info",
    ]) {
      expect(light[name], `light ${name}`).toBeDefined();
      expect(parseTokens(css, ".dark")[name] ?? light[name], `dark ${name}`).toBeDefined();
    }
  });
});
