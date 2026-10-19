/**
 * The well-known Anvil development accounts (from the public mnemonic
 * "test test test test test test test test test test test junk"). They hold test ETH on local chains
 * only. NEVER use them anywhere else; every helper that uses them calls `assertAnvil`.
 */
import type { Hex } from "viem";

export const ANVIL_CHAIN_ID = 31337;

export const ANVIL_KEYS: readonly Hex[] = [
  "0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80", // #0
  "0x59c6995e998f97a5a0044966f0945389dc9e86dae88c7a8412f4603b6b78690d", // #1
  "0x5de4111afa1a4b94908f83103eb1f1706367c2e68ca870fc3fb9a804cdab365a", // #2
  "0x7c852118294e51e653712a81e05800f419141751be58f605c371e15141b007a6", // #3
  "0x47e179ec197488593b187f80a00eb0da91f1b9d0b13f8733639f19c30a34926a", // #4
  "0x8b3a350cf5c34c9194ca85829a2df0ec3153be0318b5e2d3348e872092edffba", // #5 demo seller / meter owner
  "0x92db14e403b83dfe3df233f83dfa3a0d7096f21ca9b0d6d6b8d88b2b4ec1564e", // #6 demo buyer
  "0x4bbbf85ce3377467afe5d46f804f221813b2bb87f24d81f60f1fcdbf7cbf4356", // #7
  "0xdbda1821b80551c9d65939329250298aa3472ba22feea921c0cf5d620ea67b97", // #8
  "0x2a871d0798f97d79848a013d4936a73bf4cc922c825d33c1cf7073dff6d409c6", // #9 never used: empty account
] as const;

/** Throws unless the chain id is 31337, so a public dev key can never sign for a real network. */
export function assertAnvil(chainId: number): void {
  if (chainId !== ANVIL_CHAIN_ID) {
    throw new Error(
      `refusing to use Anvil dev keys on chain ${chainId}: they are public and only valid on chain ${ANVIL_CHAIN_ID}`,
    );
  }
}

export function anvilKey(index: number, chainId: number): Hex {
  assertAnvil(chainId);
  const key = ANVIL_KEYS[index];
  if (key === undefined) throw new RangeError(`no Anvil dev key #${index}`);
  return key;
}
