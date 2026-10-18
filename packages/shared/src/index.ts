/** Chain ids WattSwap supports. Sepolia addresses are filled in Phase 8. */
export const CHAIN_IDS = { anvil: 31337, sepolia: 11155111 } as const;

export type ChainKey = keyof typeof CHAIN_IDS;

export type ContractName =
  "EnergyToken" | "MockUSDC" | "WattSwapExchange" | "MeterRegistry" | "PriceOracle";

export type Address = `0x${string}`;

/** Deployed contract addresses per chain. Populated by the deploy scripts (Phase 1). */
export type AddressBook = Partial<Record<ContractName, Address>>;

export const ADDRESSES: Record<ChainKey, AddressBook> = {
  anvil: {},
  sepolia: {},
};

export function chainIdFor(key: ChainKey): number {
  return CHAIN_IDS[key];
}
