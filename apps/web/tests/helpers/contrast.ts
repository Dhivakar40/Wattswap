/** WCAG 2.x contrast ratio over the hsl() design tokens in globals.css. */

export type Tokens = Record<string, string>;

/** Extract `--name: value;` declarations from the first rule whose selector is exactly `selector`. */
export function parseTokens(css: string, selector: string): Tokens {
  const escaped = selector.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  const m = new RegExp(`(?:^|\\n)${escaped}\\s*\\{([\\s\\S]*?)\\n\\}`).exec(css);
  if (!m || m[1] === undefined) throw new Error(`no ${selector} rule in css`);
  const out: Tokens = {};
  for (const d of m[1].matchAll(/--([a-z0-9-]+):\s*([^;]+);/g)) {
    const [, name, value] = d;
    if (name !== undefined && value !== undefined) out[name] = value.trim();
  }
  return out;
}

export type Rgb = [number, number, number];

function hslToRgb(h: number, s: number, l: number): Rgb {
  const a = s * Math.min(l, 1 - l);
  const f = (n: number): number => {
    const k = (n + h / 30) % 12;
    return l - a * Math.max(-1, Math.min(k - 3, 9 - k, 1));
  };
  return [f(0), f(8), f(4)];
}

/** Resolve `var(--x)` chains and parse `hsl(h s% l%)` into linear-ready sRGB (0..1). */
export function resolveColor(tokens: Tokens, name: string, depth = 0): Rgb {
  const raw = tokens[name];
  if (raw === undefined) throw new Error(`unknown token --${name}`);
  if (depth > 5) throw new Error(`token cycle at --${name}`);
  const ref = /^var\(--([a-z0-9-]+)\)$/.exec(raw);
  if (ref?.[1]) return resolveColor(tokens, ref[1], depth + 1);
  const hsl = /^hsl\(\s*([\d.]+)\s+([\d.]+)%\s+([\d.]+)%\s*\)$/.exec(raw);
  if (!hsl) throw new Error(`--${name}: unsupported colour ${raw}`);
  return hslToRgb(Number(hsl[1]), Number(hsl[2]) / 100, Number(hsl[3]) / 100);
}

function luminance([r, g, b]: Rgb): number {
  const lin = (c: number): number => (c <= 0.04045 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4);
  return 0.2126 * lin(r) + 0.7152 * lin(g) + 0.0722 * lin(b);
}

export function contrastRatio(a: Rgb, b: Rgb): number {
  const [hi, lo] = [luminance(a), luminance(b)].sort((x, y) => y - x) as [number, number];
  return (hi + 0.05) / (lo + 0.05);
}
