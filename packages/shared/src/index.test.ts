import { keccak256, toBytes } from "viem";
import { describe, expect, it } from "vitest";
import {
  ADDRESSES,
  WATT_MUSDC_MARKET_ID,
  addressOf,
  chainIdFor,
  energyTokenAbi,
  mockUSDCAbi,
  wattSwapExchangeAbi,
} from "./index";

describe("shared", () => {
  it("maps chain keys to ids", () => {
    expect(chainIdFor("anvil")).toBe(31337);
    expect(chainIdFor("sepolia")).toBe(11155111);
  });

  it("has an address book per chain", () => {
    expect(Object.keys(ADDRESSES)).toEqual(["anvil", "sepolia"]);
  });

  it("exports all five contracts for the local chain", () => {
    for (const name of [
      "EnergyToken",
      "MockUSDC",
      "WattSwapExchange",
      "MeterRegistry",
      "PriceOracle",
    ] as const) {
      expect(addressOf("anvil", name)).toMatch(/^0x[0-9a-fA-F]{40}$/);
    }
  });

  it("throws for contracts that are not deployed on a chain", () => {
    expect(() => addressOf("sepolia", "EnergyToken")).toThrow(/not deployed on sepolia/);
  });

  it("market id is keccak256('WATT-mUSDC')", () => {
    expect(WATT_MUSDC_MARKET_ID).toBe(keccak256(toBytes("WATT-mUSDC")));
  });

  it("exports ABIs with the expected entry points", () => {
    const names = (abi: readonly { type: string; name?: string }[]): string[] =>
      abi.filter((e) => e.type === "function").map((e) => e.name ?? "");
    expect(names(wattSwapExchangeAbi)).toContain("settle");
    expect(names(energyTokenAbi)).toContain("mint");
    expect(names(mockUSDCAbi)).toContain("faucet");
  });
});
