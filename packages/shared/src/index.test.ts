import { describe, expect, it } from "vitest";
import { ADDRESSES, chainIdFor } from "./index";

describe("shared", () => {
  it("maps chain keys to ids", () => {
    expect(chainIdFor("anvil")).toBe(31337);
    expect(chainIdFor("sepolia")).toBe(11155111);
  });
  it("has an address book per chain", () => {
    expect(Object.keys(ADDRESSES)).toEqual(["anvil", "sepolia"]);
  });
});
