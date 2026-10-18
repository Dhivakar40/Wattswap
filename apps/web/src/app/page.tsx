import { ApiStatus } from "@/components/api-status";
import { ThemeToggle } from "@/components/theme-toggle";
import { fetchApiHealth } from "@/lib/api";

export const dynamic = "force-dynamic";

export default async function Home(): Promise<React.JSX.Element> {
  const health = await fetchApiHealth();
  return (
    <main className="mx-auto flex min-h-screen max-w-3xl flex-col justify-center gap-6 px-4 py-12">
      <div className="flex items-center justify-between">
        <h1 className="text-3xl font-semibold tracking-tight">WattSwap</h1>
        <ThemeToggle />
      </div>
      <p className="text-muted-foreground">
        A peer-to-peer marketplace for tokenized surplus solar energy. This is a testnet demo:
        meters are simulated and payments use a mock stablecoin. No physical energy is delivered and
        no real money moves.
      </p>
      <ApiStatus health={health} />
    </main>
  );
}
