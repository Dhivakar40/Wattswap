/**
 * Hand-built EIP-4361 (Sign-In with Ethereum) message. We do not depend on the `siwe` npm package
 * (it pulls in ethers). Correctness is enforced by a cross-language test: messages produced here are
 * parsed, re-serialised byte-for-byte and signature-verified by the backend's Python `siwe` library
 * (packages/shared/vectors/siwe-frontend.json; tests/siwe-fixture.test.ts and
 * services/api/tests/unit/test_siwe_frontend_fixture.py).
 */

import { getAddress, type Address } from "viem";

export interface SiweFields {
  /** RFC 3986 authority of the requesting site, e.g. "localhost:3000". */
  domain: string;
  address: string;
  /** Optional URI scheme of the requesting site ("https"). */
  scheme?: string;
  statement?: string;
  uri: string;
  chainId: number;
  nonce: string;
  issuedAt: Date;
  expirationTime?: Date;
  notBefore?: Date;
  requestId?: string;
  resources?: string[];
}

export class SiweError extends Error {}

const NO_LF = /[\r\n]/;

function line(name: string, value: string): string {
  if (NO_LF.test(value)) throw new SiweError(`${name} must not contain line breaks`);
  return value;
}

export const SIWE_STATEMENT = "Sign in to WattSwap (testnet demo).";

export function buildSiweMessage(f: SiweFields): string {
  if (!Number.isSafeInteger(f.chainId) || f.chainId <= 0) throw new SiweError("invalid chain id");
  if (!/^[a-zA-Z0-9]{8,}$/.test(f.nonce)) throw new SiweError("nonce must be >= 8 alphanumerics");
  const address: Address = getAddress(f.address); // EIP-55 checksummed, as EIP-4361 requires
  const head = `${f.scheme !== undefined ? `${line("scheme", f.scheme)}://` : ""}${line("domain", f.domain)}`;
  const lines = [
    `${head} wants you to sign in with your Ethereum account:`,
    address,
    "",
    f.statement !== undefined ? line("statement", f.statement) : "",
    "",
    `URI: ${line("uri", f.uri)}`,
    "Version: 1",
    `Chain ID: ${f.chainId}`,
    `Nonce: ${f.nonce}`,
    `Issued At: ${f.issuedAt.toISOString()}`,
  ];
  // Without a statement the ABNF is `address LF LF LF URI`: keep both blank lines, drop the text line.
  const body = f.statement === undefined ? [...lines.slice(0, 3), ...lines.slice(4)] : lines;
  if (f.expirationTime) body.push(`Expiration Time: ${f.expirationTime.toISOString()}`);
  if (f.notBefore) body.push(`Not Before: ${f.notBefore.toISOString()}`);
  if (f.requestId !== undefined) body.push(`Request ID: ${line("request id", f.requestId)}`);
  if (f.resources && f.resources.length > 0) {
    body.push("Resources:");
    for (const r of f.resources) body.push(`- ${line("resource", r)}`);
  }
  return body.join("\n");
}

/** The fields the app uses for its own login message. */
export function loginFields(args: {
  address: string;
  chainId: number;
  nonce: string;
  origin: string;
  now?: Date;
  ttlMinutes?: number;
}): SiweFields {
  const url = new URL(args.origin);
  const now = args.now ?? new Date();
  return {
    domain: url.host,
    address: args.address,
    statement: SIWE_STATEMENT,
    uri: url.origin,
    chainId: args.chainId,
    nonce: args.nonce,
    issuedAt: now,
    expirationTime: new Date(now.getTime() + (args.ttlMinutes ?? 5) * 60_000),
  };
}
