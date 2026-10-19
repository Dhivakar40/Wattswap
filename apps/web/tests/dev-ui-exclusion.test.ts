import { existsSync } from "node:fs";
import { resolve } from "node:path";
import { afterEach, describe, expect, it, vi } from "vitest";

const load = async (
  devUi: string | undefined,
): Promise<{ pageExtensions: string[]; distDir: string }> => {
  vi.resetModules();
  if (devUi === undefined) vi.stubEnv("WEB_DEV_UI", "");
  else vi.stubEnv("WEB_DEV_UI", devUi);
  const mod = (await import(/* @vite-ignore */ `../next.config.mjs?${Math.random()}`)) as {
    default: { pageExtensions: string[]; distDir: string };
  };
  return mod.default;
};

afterEach(() => vi.unstubAllEnvs());

describe("/dev/ui is not part of production builds", () => {
  it("the page file only matches the dev.tsx page extension", () => {
    expect(existsSync(resolve(__dirname, "../src/app/dev/ui/page.dev.tsx"))).toBe(true);
    expect(existsSync(resolve(__dirname, "../src/app/dev/ui/page.tsx"))).toBe(false);
  });

  it("a normal build does not register the dev.tsx extension", async () => {
    const cfg = await load(undefined);
    expect(cfg.pageExtensions).toEqual(["tsx", "ts"]);
    expect(cfg.pageExtensions).not.toContain("dev.tsx");
  });

  it("only WEB_DEV_UI=1 turns it on", async () => {
    expect((await load("1")).pageExtensions).toContain("dev.tsx");
    expect((await load("true")).pageExtensions).not.toContain("dev.tsx");
    expect((await load("0")).pageExtensions).not.toContain("dev.tsx");
  });
});
