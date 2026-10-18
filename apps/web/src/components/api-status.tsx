import type { ApiHealth } from "@/lib/api";

export function ApiStatus({ health }: { health: ApiHealth }): React.JSX.Element {
  return (
    <section aria-label="Backend status" className="rounded-lg border bg-card p-4">
      <h2 className="text-sm font-medium text-muted-foreground">Backend API</h2>
      {health.ok ? (
        <p role="status" className="mt-1 font-medium text-success">
          Online ({health.status})
        </p>
      ) : (
        <p role="alert" className="mt-1 font-medium text-destructive">
          Unreachable: {health.error}
        </p>
      )}
    </section>
  );
}
