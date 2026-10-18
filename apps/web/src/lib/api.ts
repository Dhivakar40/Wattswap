export type ApiHealth = { ok: true; status: string } | { ok: false; error: string };

/** Server-side health probe against the FastAPI service. Never throws. */
export async function fetchApiHealth(): Promise<ApiHealth> {
  const base =
    process.env.API_INTERNAL_URL ?? process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";
  try {
    const res = await fetch(`${base}/health`, {
      cache: "no-store",
      signal: AbortSignal.timeout(3000),
    });
    if (!res.ok) return { ok: false, error: `HTTP ${res.status}` };
    const body = (await res.json()) as { status?: string };
    return { ok: true, status: body.status ?? "unknown" };
  } catch (e) {
    return { ok: false, error: e instanceof Error ? e.message : "unreachable" };
  }
}
