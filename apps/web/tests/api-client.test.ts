import { afterEach, describe, expect, it, vi } from "vitest";
import {
  ApiError,
  channelPosition,
  createApi,
  onUnauthorized,
  toApiError,
  unwrap,
} from "@/lib/api/client";
import { isUserRejection } from "@/lib/auth/sign-in";

const json = (status: number, body: unknown, headers: Record<string, string> = {}): Response =>
  new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json", ...headers },
  });
const asFetch = (fn: (req: Request) => Promise<Response>): typeof fetch =>
  fn as unknown as typeof fetch;

afterEach(() => vi.restoreAllMocks());

describe("error envelope", () => {
  it("maps the API error envelope", () => {
    const e = toApiError(409, {
      error: {
        code: "nonce_used",
        message: "nonce already used",
        request_id: "r1",
        details: { a: 1 },
      },
    });
    expect(e).toBeInstanceOf(ApiError);
    expect([e.status, e.code, e.message, e.requestId, e.details]).toEqual([
      409,
      "nonce_used",
      "nonce already used",
      "r1",
      { a: 1 },
    ]);
  });

  it("falls back for bodies that are not an envelope", () => {
    expect(toApiError(502, "<html>").code).toBe("http_error");
    expect(toApiError(500, { detail: "x" }).message).toContain("500");
  });
});

describe("typed client", () => {
  it("sends credentials and unwraps data", async () => {
    const fetchImpl = vi.fn(async (_req: Request) =>
      json(200, { nonce: "abcdefgh12345678", expires_in: 300 }),
    );
    const api = createApi("http://api.test", asFetch(fetchImpl));
    const { data } = await unwrap(api.GET("/api/v1/auth/nonce"));
    expect(data.nonce).toBe("abcdefgh12345678");
    const req = fetchImpl.mock.calls[0]?.[0] as Request;
    expect(req.url).toBe("http://api.test/api/v1/auth/nonce");
    expect(req.credentials).toBe("include");
  });

  it("serialises query parameters from the generated types", async () => {
    const fetchImpl = vi.fn(async (_req: Request) => json(200, {}));
    const api = createApi("http://api.test", asFetch(fetchImpl));
    await api.GET("/api/v1/portfolio", { params: { query: { period: "7d" } } });
    expect((fetchImpl.mock.calls[0]?.[0] as Request).url).toBe(
      "http://api.test/api/v1/portfolio?period=7d",
    );
  });

  it("throws an ApiError carrying the envelope and request id", async () => {
    const api = createApi(
      "http://api.test",
      asFetch(async () =>
        json(422, { error: { code: "validation_error", message: "bad", request_id: "rq" } }),
      ),
    );
    await expect(unwrap(api.GET("/api/v1/portfolio"))).rejects.toMatchObject({
      status: 422,
      code: "validation_error",
      requestId: "rq",
    });
  });

  it("turns a network failure into an ApiError", async () => {
    const api = createApi(
      "http://api.test",
      asFetch(async () => {
        throw new TypeError("fetch failed");
      }),
    );
    await expect(unwrap(api.GET("/api/v1/portfolio"))).rejects.toMatchObject({
      code: "network_error",
      status: 0,
    });
  });

  it("notifies on a 401 from a data endpoint but not from /auth/*", async () => {
    const api = createApi(
      "http://api.test",
      asFetch(async () => json(401, { error: { code: "unauthorized", message: "no" } })),
    );
    const listener = vi.fn();
    const off = onUnauthorized(listener);
    await api.GET("/api/v1/auth/me");
    expect(listener).not.toHaveBeenCalled();
    await api.GET("/api/v1/portfolio");
    expect(listener).toHaveBeenCalledTimes(1);
    off();
  });
});

describe("channelPosition", () => {
  it("reads the epoch and seq the server read before the snapshot", () => {
    const r = json(200, [], { "x-channel-epoch": "e1", "x-channel-seq": "41" });
    expect(channelPosition(r)).toEqual({ epoch: "e1", seq: 41 });
  });

  it("refuses a snapshot without a position", () => {
    expect(() => channelPosition(json(200, []))).toThrow(ApiError);
    const bad = json(200, [], { "x-channel-epoch": "e", "x-channel-seq": "-1" });
    expect(() => channelPosition(bad)).toThrow(RangeError);
  });
});

describe("isUserRejection", () => {
  it("recognises wallet rejections however they are wrapped", () => {
    expect(isUserRejection({ code: 4001 })).toBe(true);
    expect(isUserRejection({ name: "UserRejectedRequestError" })).toBe(true);
    expect(isUserRejection(new Error("User rejected the request."))).toBe(true);
    expect(isUserRejection({ message: "wrapped", cause: { code: 4001 } })).toBe(true);
    expect(isUserRejection(new Error("network down"))).toBe(false);
    expect(isUserRejection(null)).toBe(false);
  });
});
