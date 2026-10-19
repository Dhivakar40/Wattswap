/**
 * A scripted browser wallet for Playwright: an EIP-1193 provider injected into the page whose requests
 * are answered in Node with one of Anvil's public dev keys. TEST ONLY, never imported by `src/`.
 *
 * Safety: the factory throws unless the app under test is configured for chain 31337, and every
 * signing method re-checks that the wallet is currently on 31337, so a public key can never sign
 * anything for another network even if a test mis-configures the page.
 */

import type { Page } from "@playwright/test";
import { hexToString, isHex, toHex, type Hex, type TypedDataDefinition } from "viem";
import { privateKeyToAccount, type PrivateKeyAccount } from "viem/accounts";
import { ANVIL_CHAIN_ID, anvilKey, assertAnvil } from "../helpers/dev-keys";

export interface TestWalletOptions {
  /** Which Anvil dev account (0-9). */
  keyIndex: number;
  /** The chain id the app under test is configured for (NEXT_PUBLIC_CHAIN_ID). MUST be 31337. */
  appChainId: number;
  /** The chain the wallet starts on. Default: the app's. Use another id to test wrong-network. */
  walletChainId?: number;
  rpcUrl?: string;
  /** Reject signature requests (to test the declined-in-wallet path). */
  rejectSignatures?: boolean;
  /** Start with the site already authorised (like a wallet that remembers the permission). */
  preAuthorized?: boolean;
}

export interface TestWallet {
  address: string;
  account: PrivateKeyAccount;
  state: { chainId: number; rejectSignatures: boolean; signed: string[]; authorized: boolean };
  install(page: Page): Promise<void>;
  /** The user changes network inside the wallet UI (emits chainChanged to the page). */
  userSwitchesChain(chainId: number): Promise<void>;
  /** Handle one EIP-1193 request (exported for unit tests). */
  request(method: string, params: unknown[]): Promise<unknown>;
}

class RpcError extends Error {
  constructor(
    readonly code: number,
    message: string,
  ) {
    super(message);
  }
}

export function createTestWallet(opts: TestWalletOptions): TestWallet {
  assertAnvil(opts.appChainId); // throws unless the app is on Anvil
  const account = privateKeyToAccount(anvilKey(opts.keyIndex, opts.appChainId));
  const rpcUrl = opts.rpcUrl ?? "http://127.0.0.1:8545";
  const state = {
    chainId: opts.walletChainId ?? opts.appChainId,
    rejectSignatures: opts.rejectSignatures ?? false,
    signed: [] as string[],
    authorized: opts.preAuthorized ?? false,
  };
  let emit: ((event: string, payload: unknown) => Promise<void>) | undefined;

  async function forward(method: string, params: unknown[]): Promise<unknown> {
    const res = await fetch(rpcUrl, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ jsonrpc: "2.0", id: 1, method, params }),
    });
    const body = (await res.json()) as {
      result?: unknown;
      error?: { code: number; message: string };
    };
    if (body.error) throw new RpcError(body.error.code, body.error.message);
    return body.result;
  }

  function guardSigning(): void {
    assertAnvil(state.chainId); // the wallet must currently be on Anvil to use a dev key
    if (state.rejectSignatures) throw new RpcError(4001, "User rejected the request.");
  }

  async function request(method: string, params: unknown[]): Promise<unknown> {
    switch (method) {
      case "eth_chainId":
        return toHex(state.chainId);
      case "net_version":
        return String(state.chainId);
      case "eth_accounts":
        return state.authorized ? [account.address] : [];
      case "eth_requestAccounts":
        state.authorized = true;
        return [account.address];
      case "wallet_requestPermissions":
      case "wallet_getPermissions":
        return [{ parentCapability: "eth_accounts" }];
      case "wallet_switchEthereumChain": {
        const { chainId } = (params[0] ?? {}) as { chainId?: string };
        if (typeof chainId !== "string") throw new RpcError(-32602, "chainId required");
        state.chainId = Number(BigInt(chainId));
        await emit?.("chainChanged", toHex(state.chainId));
        return null;
      }
      case "wallet_addEthereumChain":
        return null;
      case "personal_sign": {
        guardSigning();
        const [data, from] = params as [string, string];
        if (from.toLowerCase() !== account.address.toLowerCase())
          throw new RpcError(4100, "unknown account");
        const message = isHex(data) ? hexToString(data as Hex) : data;
        state.signed.push(message);
        return account.signMessage({ message });
      }
      case "eth_signTypedData_v4": {
        guardSigning();
        const typed = JSON.parse(params[1] as string) as TypedDataDefinition;
        state.signed.push(JSON.stringify(typed));
        return account.signTypedData(typed);
      }
      case "eth_sendTransaction":
        throw new RpcError(4200, "the QA wallet does not send transactions");
      default:
        return forward(method, params); // reads: eth_call, eth_getBalance, eth_blockNumber, ...
    }
  }

  async function install(page: Page): Promise<void> {
    await page.exposeFunction("__qaWalletRequest", async (method: string, params: unknown[]) => {
      try {
        return { ok: true, result: await request(method, params ?? []) };
      } catch (e) {
        const err = e as { code?: number; message?: string };
        return { ok: false, code: err.code ?? -32603, message: err.message ?? "error" };
      }
    });
    emit = async (event, payload): Promise<void> => {
      await page.evaluate(
        ([ev, data]) =>
          (window as unknown as { __qaWalletEmit(e: string, p: unknown): void }).__qaWalletEmit(
            ev as string,
            data,
          ),
        [event, payload] as const,
      );
    };
    await page.addInitScript(
      ({ address }) => {
        const listeners = new Map<string, Set<(p: unknown) => void>>();
        const w = window as unknown as Record<string, unknown>;
        const provider = {
          isMetaMask: true,
          selectedAddress: address,
          async request({
            method,
            params,
          }: {
            method: string;
            params?: unknown[];
          }): Promise<unknown> {
            const r = await (
              w.__qaWalletRequest as (
                m: string,
                p: unknown[],
              ) => Promise<{ ok: boolean; result?: unknown; code?: number; message?: string }>
            )(method, params ?? []);
            if (r.ok) return r.result;
            throw Object.assign(new Error(r.message ?? "wallet error"), { code: r.code });
          },
          on(event: string, fn: (p: unknown) => void): void {
            if (!listeners.has(event)) listeners.set(event, new Set());
            listeners.get(event)?.add(fn);
          },
          removeListener(event: string, fn: (p: unknown) => void): void {
            listeners.get(event)?.delete(fn);
          },
        };
        w.ethereum = provider;
        w.__qaWalletEmit = (event: string, payload: unknown): void => {
          listeners.get(event)?.forEach((fn) => fn(payload));
        };
        // EIP-6963 so wagmi/RainbowKit find it without relying on window.ethereum alone
        const info = {
          uuid: "qa-wallet",
          name: "QA Test Wallet",
          icon: "data:image/svg+xml;base64,PHN2ZyB4bWxucz0iaHR0cDovL3d3dy53My5vcmcvMjAwMC9zdmciIHdpZHRoPSIzMiIgaGVpZ2h0PSIzMiI+PC9zdmc+",
          rdns: "dev.wattswap.qa",
        };
        const announce = (): void => {
          window.dispatchEvent(
            new CustomEvent("eip6963:announceProvider", {
              detail: Object.freeze({ info, provider }),
            }),
          );
        };
        window.addEventListener("eip6963:requestProvider", announce);
        announce();
      },
      { address: account.address },
    );
  }

  async function userSwitchesChain(chainId: number): Promise<void> {
    state.chainId = chainId;
    await emit?.("chainChanged", toHex(chainId));
  }

  return { address: account.address, account, state, install, request, userSwitchesChain };
}

export { ANVIL_CHAIN_ID };
