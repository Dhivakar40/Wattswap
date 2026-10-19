import { connectorsForWallets } from "@rainbow-me/rainbowkit";
import { injectedWallet, rainbowWallet, walletConnectWallet } from "@rainbow-me/rainbowkit/wallets";
import { createConfig, http } from "wagmi";
import { defineChain, type Chain } from "viem";
import { config } from "@/lib/config";

const ETH = { name: "Ether", symbol: "ETH", decimals: 18 } as const;

// Defined here instead of importing `viem/chains`: that barrel pulls ~700 chain definitions (and a
// build warning) into the bundle for the two chains we use.
/** The one chain this build talks to (the deployed addresses and the API's CHAIN_ID agree on it). */
export function appChain(): Chain {
  if (config.chainKey === "sepolia") {
    return defineChain({
      id: 11155111,
      name: "Sepolia",
      nativeCurrency: { ...ETH, name: "Sepolia Ether" },
      rpcUrls: { default: { http: [config.rpcUrl] } },
      blockExplorers: { default: { name: "Etherscan", url: "https://sepolia.etherscan.io" } },
      testnet: true,
    });
  }
  return defineChain({
    id: 31337,
    name: "Anvil (local)",
    nativeCurrency: ETH,
    rpcUrls: { default: { http: [config.rpcUrl] } },
    testnet: true,
  });
}

export const chain = appChain();

export const NETWORK_LABELS = {
  anvil: { name: "Anvil", detail: "local", explorer: undefined },
  sepolia: { name: "Sepolia", detail: "testnet", explorer: "https://sepolia.etherscan.io" },
} as const;

/** Injected wallets always; WalletConnect-based ones only when a project id is configured. */
function connectors(): ReturnType<typeof connectorsForWallets> {
  const projectId = config.walletConnectProjectId;
  const groups = [
    {
      groupName: "Browser wallets",
      wallets: [injectedWallet],
    },
    ...(projectId
      ? [{ groupName: "Mobile wallets", wallets: [rainbowWallet, walletConnectWallet] }]
      : []),
  ];
  return connectorsForWallets(groups, {
    appName: "WattSwap",
    projectId: projectId ?? "wattswap-no-walletconnect",
  });
}

export function makeWagmiConfig(): ReturnType<typeof createConfig> {
  return createConfig({
    chains: [chain],
    connectors: connectors(),
    transports: { [chain.id]: http(config.rpcUrl) },
    ssr: true,
  });
}

/** Block explorer link for a tx or address on the configured chain; undefined on local Anvil. */
export function explorerUrl(kind: "tx" | "address", value: string): string | undefined {
  const base = NETWORK_LABELS[config.chainKey].explorer;
  return base === undefined ? undefined : `${base}/${kind}/${value}`;
}
