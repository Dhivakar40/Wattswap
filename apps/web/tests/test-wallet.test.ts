import { verifyMessage } from "viem";
import { describe, expect, it } from "vitest";
import { createTestWallet } from "./e2e-visual/test-wallet";

describe("QA test wallet safety", () => {
  it("refuses to be created for any chain but Anvil", () => {
    expect(() => createTestWallet({ keyIndex: 0, appChainId: 1 })).toThrow(/refusing/);
    expect(() => createTestWallet({ keyIndex: 0, appChainId: 11155111 })).toThrow(/refusing/);
  });

  it("signs personal_sign with the Anvil key while on chain 31337", async () => {
    const w = createTestWallet({ keyIndex: 5, appChainId: 31337 });
    expect(await w.request("eth_chainId", [])).toBe("0x7a69");
    const msg = "hello wattswap";
    const sig = (await w.request("personal_sign", [
      Buffer.from(msg).toString("hex").replace(/^/, "0x"),
      w.address,
    ])) as `0x${string}`;
    expect(
      await verifyMessage({ address: w.address as `0x${string}`, message: msg, signature: sig }),
    ).toBe(true);
  });

  it("will not sign while the wallet sits on another chain, even with a valid Anvil key", async () => {
    const w = createTestWallet({ keyIndex: 0, appChainId: 31337, walletChainId: 1 });
    await expect(w.request("personal_sign", ["0x00", w.address])).rejects.toThrow(/refusing/);
    await w.request("wallet_switchEthereumChain", [{ chainId: "0x7a69" }]);
    await expect(w.request("personal_sign", ["0x00", w.address])).resolves.toMatch(/^0x/);
  });

  it("can simulate a declined signature (EIP-1193 code 4001)", async () => {
    const w = createTestWallet({ keyIndex: 0, appChainId: 31337, rejectSignatures: true });
    await expect(w.request("personal_sign", ["0x00", w.address])).rejects.toMatchObject({
      code: 4001,
    });
  });

  it("never sends transactions", async () => {
    const w = createTestWallet({ keyIndex: 0, appChainId: 31337 });
    await expect(w.request("eth_sendTransaction", [{}])).rejects.toMatchObject({ code: 4200 });
  });
});
