import type { ApiHealth } from "@/lib/api";

export function ApiStatus({ health }: { health: ApiHealth }): React.JSX.Element {
  return (
    <section aria-label="Backend status" className="bg-card rounded-lg border p-4">
      <h2 className="text-muted-foreground text-sm font-medium">Backend API</h2>
      {health.ok ? (
        <p role="status" className="text-success mt-1 font-medium">
          Online ({health.status})
        </p>
      ) : (
        <p role="alert" className="text-destructive mt-1 font-medium">
          Unreachable: {health.error}
        </p>
      )}
    </section>
  );
}
