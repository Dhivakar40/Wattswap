/** SIWE sign-in against the API: nonce -> EIP-4361 message -> wallet signature -> session cookie. */

import { api, ApiError, toApiError, unwrap } from "@/lib/api/client";
import type { SessionUser } from "./session";
import { buildSiweMessage, loginFields } from "./siwe";

export class SignInRejectedError extends Error {
  constructor() {
    super("The signature request was declined in your wallet.");
    this.name = "SignInRejectedError";
  }
}

/** Wallets reject with EIP-1193 code 4001 or a message containing "rejected"/"denied". */
export function isUserRejection(e: unknown): boolean {
  if (typeof e !== "object" || e === null) return false;
  const { code, name, message, cause } = e as {
    code?: unknown;
    name?: unknown;
    message?: unknown;
    cause?: unknown;
  };
  if (code === 4001 || code === "ACTION_REJECTED") return true;
  if (name === "UserRejectedRequestError") return true;
  if (typeof message === "string" && /user (rejected|denied)|rejected the request/i.test(message)) {
    return true;
  }
  return cause !== undefined && cause !== e ? isUserRejection(cause) : false;
}

export interface SignInDeps {
  address: string;
  chainId: number;
  origin: string;
  signMessage(message: string): Promise<string>;
  now?: () => Date;
}

export async function signInWithEthereum(deps: SignInDeps): Promise<SessionUser> {
  const { data: nonce } = await unwrap(api.GET("/api/v1/auth/nonce"));
  const message = buildSiweMessage(
    loginFields({
      address: deps.address,
      chainId: deps.chainId,
      nonce: nonce.nonce,
      origin: deps.origin,
      ...(deps.now ? { now: deps.now() } : {}),
    }),
  );
  let signature: string;
  try {
    signature = await deps.signMessage(message);
  } catch (e) {
    if (isUserRejection(e)) throw new SignInRejectedError();
    throw e;
  }
  const { data } = await unwrap(api.POST("/api/v1/auth/login", { body: { message, signature } }));
  return data;
}

export async function fetchSessionUser(): Promise<SessionUser | null> {
  try {
    const { data } = await unwrap(api.GET("/api/v1/auth/me"));
    return data;
  } catch (e) {
    if (e instanceof ApiError && e.status === 401) return null;
    throw e;
  }
}

export async function signOutFromApi(): Promise<void> {
  const { response, error } = await api.POST("/api/v1/auth/logout");
  // 204 = revoked. 401 = the session was already gone, which is the outcome we wanted.
  if (!response.ok && response.status !== 401) {
    throw toApiError(response.status, error, response.headers.get("x-request-id") ?? undefined);
  }
}
