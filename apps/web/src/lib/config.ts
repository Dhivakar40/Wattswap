/**
 * Typed runtime config from NEXT_PUBLIC_* env (inlined at build time) and the committed
 * `@wattswap/shared` addresses. Nothing secret lives here: it is all shipped to the browser.
 */

import { ADDRESSES, CHAIN_IDS, type Address, type ChainKey } from "@wattswap/shared";

export { CHAIN_IDS };
export type { ChainKey };

export interface DeployedAddresses {
  EnergyToken: Address;
  MockUSDC: Address;
  WattSwapExchange: Address;
  MeterRegistry: Address;
  PriceOracle: Address;
}

function int(value: string | undefined, fallback: number): number {
  if (value === undefined || value === "") return fallback;
  const n = Number(value);
  if (!Number.isSafeInteger(n) || n <= 0) throw new Error(`invalid integer env value: ${value}`);
  return n;
}

export interface AppConfig {
  apiUrl: string;
  wsUrl: string;
  chainId: number;
  chainKey: ChainKey;
  rpcUrl: string;
  walletConnectProjectId: string | undefined;
  /** Contract addresses for the configured chain; undefined if that chain has no deployment yet. */
  contracts: DeployedAddresses | undefined;
}

export function buildConfig(env: Record<string, string | undefined>): AppConfig {
  const apiUrl = (env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000").replace(/\/+$/, "");
  const chainId = int(env.NEXT_PUBLIC_CHAIN_ID, CHAIN_IDS.anvil);
  const chainKey: ChainKey | undefined = (Object.keys(CHAIN_IDS) as ChainKey[]).find(
    (k) => CHAIN_IDS[k] === chainId,
  );
  if (chainKey === undefined) throw new Error(`unsupported NEXT_PUBLIC_CHAIN_ID ${chainId}`);
  const wsUrl = env.NEXT_PUBLIC_WS_URL ?? `${apiUrl.replace(/^http/, "ws")}/ws`; // http->ws, https->wss
  const d = ADDRESSES[chainKey];
  const complete =
    d.EnergyToken && d.MockUSDC && d.WattSwapExchange && d.MeterRegistry && d.PriceOracle
      ? {
          EnergyToken: d.EnergyToken,
          MockUSDC: d.MockUSDC,
          WattSwapExchange: d.WattSwapExchange,
          MeterRegistry: d.MeterRegistry,
          PriceOracle: d.PriceOracle,
        }
      : undefined;
  return {
    apiUrl,
    wsUrl,
    chainId,
    chainKey,
    rpcUrl:
      env.NEXT_PUBLIC_RPC_URL ??
      (chainKey === "anvil" ? "http://127.0.0.1:8545" : "https://rpc.sepolia.org"),
    walletConnectProjectId: env.NEXT_PUBLIC_WALLETCONNECT_PROJECT_ID || undefined,
    contracts: complete,
  };
}

// Next only inlines `process.env.NEXT_PUBLIC_X` when written out in full, so list them explicitly.
export const config: AppConfig = buildConfig({
  NEXT_PUBLIC_API_URL: process.env.NEXT_PUBLIC_API_URL,
  NEXT_PUBLIC_WS_URL: process.env.NEXT_PUBLIC_WS_URL,
  NEXT_PUBLIC_CHAIN_ID: process.env.NEXT_PUBLIC_CHAIN_ID,
  NEXT_PUBLIC_RPC_URL: process.env.NEXT_PUBLIC_RPC_URL,
  NEXT_PUBLIC_WALLETCONNECT_PROJECT_ID: process.env.NEXT_PUBLIC_WALLETCONNECT_PROJECT_ID,
});
