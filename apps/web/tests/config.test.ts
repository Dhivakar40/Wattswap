import { describe, expect, it } from "vitest";
import { buildConfig } from "@/lib/config";
import { parsePriceView, primaryMarket } from "@/lib/hooks/market";

describe("config", () => {
  it("defaults to local Anvil with the committed addresses", () => {
    const c = buildConfig({});
    expect(c.chainId).toBe(31337);
    expect(c.chainKey).toBe("anvil");
    expect(c.apiUrl).toBe("http://localhost:8000");
    expect(c.wsUrl).toBe("ws://localhost:8000/ws");
    expect(c.contracts?.WattSwapExchange).toMatch(/^0x[0-9a-fA-F]{40}$/);
  });

  it("derives wss from an https API and strips trailing slashes", () => {
    expect(buildConfig({ NEXT_PUBLIC_API_URL: "https://api.example.com/" }).wsUrl).toBe(
      "wss://api.example.com/ws",
    );
  });

  it("reports no contracts for a chain that is not deployed yet (Sepolia until Phase 8)", () => {
    const c = buildConfig({ NEXT_PUBLIC_CHAIN_ID: "11155111" });
    expect(c.chainKey).toBe("sepolia");
    expect(c.contracts).toBeUndefined();
  });

  it("rejects chains the app does not support and malformed ids", () => {
    expect(() => buildConfig({ NEXT_PUBLIC_CHAIN_ID: "1" })).toThrow(/unsupported/);
    expect(() => buildConfig({ NEXT_PUBLIC_CHAIN_ID: "abc" })).toThrow(/invalid/);
  });

  it("treats an empty WalletConnect id as unset", () => {
    expect(
      buildConfig({ NEXT_PUBLIC_WALLETCONNECT_PROJECT_ID: "" }).walletConnectProjectId,
    ).toBeUndefined();
  });
});

describe("price parsing", () => {
  it("reads the fields the UI uses and ignores the rest", () => {
    const p = parsePriceView({
      price: 597_200,
      bandBps: 150,
      breaker: "NORMAL",
      weatherStale: false,
      ts: 5,
      drivers: { x: 1 },
    });
    expect(p).toMatchObject({
      price: 597_200n,
      bandBps: 150,
      breaker: "NORMAL",
      halted: false,
      ts: 5,
    });
  });
  it("rejects a body without a price or timestamp", () => {
    expect(() => parsePriceView(null)).toThrow(TypeError);
    expect(() => parsePriceView({ ts: 1 })).toThrow(/price/);
    expect(() => parsePriceView({ price: 1 })).toThrow(/ts/);
    expect(() => parsePriceView({ price: 1.5, ts: 1 })).toThrow(RangeError);
  });
  it("picks the primary enabled market, else the first enabled", () => {
    const m = (id: string, enabled: boolean, primary: boolean) =>
      ({ market_id: id, name: id, enabled, is_primary: primary, region: {} }) as never;
    expect(primaryMarket([m("a", true, false), m("b", true, true)])?.market_id).toBe("b");
    expect(primaryMarket([m("a", true, false), m("b", false, true)])?.market_id).toBe("a");
    expect(primaryMarket([])).toBeUndefined();
  });
});
