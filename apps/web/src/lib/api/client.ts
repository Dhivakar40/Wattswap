/**
 * Typed API client. `schema.d.ts` is GENERATED from the FastAPI OpenAPI schema
 * (`make gen-api`; CI fails if the committed copy is stale), so every path, query and body below is
 * checked against the backend at compile time.
 */

import createClient, { type Middleware } from "openapi-fetch";
import { config } from "@/lib/config";
import type { paths } from "./schema";

export interface ErrorEnvelope {
  error: { code: string; message: string; details?: unknown; request_id?: string };
}

export class ApiError extends Error {
  constructor(
    readonly status: number,
    readonly code: string,
    message: string,
    readonly requestId: string | undefined,
    readonly details?: unknown,
  ) {
    super(message);
    this.name = "ApiError";
  }

  get isUnauthorized(): boolean {
    return this.status === 401;
  }
}

function isEnvelope(body: unknown): body is ErrorEnvelope {
  if (typeof body !== "object" || body === null) return false;
  const e = (body as { error?: unknown }).error;
  return (
    typeof e === "object" &&
    e !== null &&
    typeof (e as { code?: unknown }).code === "string" &&
    typeof (e as { message?: unknown }).message === "string"
  );
}

/** Turn any failed response (or network failure) into an ApiError. */
export function toApiError(status: number, body: unknown, requestId?: string): ApiError {
  if (isEnvelope(body)) {
    const { code, message, details, request_id } = body.error;
    return new ApiError(status, code, message, request_id ?? requestId, details);
  }
  return new ApiError(status, "http_error", `request failed with HTTP ${status}`, requestId);
}

type Listener = () => void;
const unauthorizedListeners = new Set<Listener>();

/** Called when an authenticated request comes back 401 (session expired or revoked). */
export function onUnauthorized(listener: Listener): () => void {
  unauthorizedListeners.add(listener);
  return () => unauthorizedListeners.delete(listener);
}

const watch401: Middleware = {
  onResponse({ response, request }) {
    // 401 from the /auth/* endpoints is part of normal sign-in and sign-out, not an expired session.
    if (response.status === 401 && !new URL(request.url).pathname.includes("/auth/")) {
      for (const l of unauthorizedListeners) l();
    }
    return undefined;
  },
};

export function createApi(
  baseUrl: string,
  fetchImpl?: typeof fetch,
): ReturnType<typeof createClient<paths>> {
  const client = createClient<paths>({
    baseUrl,
    credentials: "include",
    ...(fetchImpl ? { fetch: fetchImpl } : {}),
  });
  client.use(watch401);
  return client;
}

export const api = createApi(config.apiUrl);

interface Result<T> {
  data?: T;
  error?: unknown;
  response: Response;
}

/** Unwrap an openapi-fetch result: return `data` + the raw response, or throw an ApiError. */
export async function unwrap<T>(
  promise: Promise<Result<T>>,
): Promise<{ data: T; response: Response }> {
  let result: Result<T>;
  try {
    result = await promise;
  } catch (e) {
    throw new ApiError(
      0,
      "network_error",
      e instanceof Error ? e.message : "network error",
      undefined,
    );
  }
  const { data, error, response } = result;
  if (!response.ok || data === undefined) {
    throw toApiError(response.status, error, response.headers.get("x-request-id") ?? undefined);
  }
  return { data, response };
}

/** The (epoch, seq) the server read BEFORE the snapshot data, from the response headers. */
export function channelPosition(response: Response): { epoch: string; seq: number } {
  const epoch = response.headers.get("x-channel-epoch");
  const seq = response.headers.get("x-channel-seq");
  if (epoch === null || seq === null) {
    throw new ApiError(
      response.status,
      "missing_channel_headers",
      "snapshot lacks X-Channel-* headers",
      undefined,
    );
  }
  const n = Number(seq);
  if (!Number.isSafeInteger(n) || n < 0) throw new RangeError(`bad X-Channel-Seq: ${seq}`);
  return { epoch, seq: n };
}
