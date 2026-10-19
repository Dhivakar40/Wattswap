import Link from "next/link";
import { ArrowRight, Gauge, Layers, Radio, Sun } from "lucide-react";
import { LandingPrice } from "@/components/landing/landing-price";
import { buttonVariants } from "@/components/ui/button";
import { Card, CardBody } from "@/components/ui/primitives";

const PROPERTIES = [
  {
    icon: Sun,
    title: "Tokenized surplus",
    body: "Verified surplus generation is minted as WATT, an ERC-20 where 1.000 WATT is 1 kWh. Each mint is backed by a signed meter reading and can only happen once.",
  },
  {
    icon: Gauge,
    title: "Weather-driven price",
    body: "A pricing engine re-prices the market from live solar irradiance, cloud cover and order-book supply and demand, then damps the volatility.",
  },
  {
    icon: Layers,
    title: "Atomic settlement",
    body: "A trade is one transaction: energy tokens go to the buyer and payment goes to the seller together, or nothing moves at all.",
  },
  {
    icon: Radio,
    title: "Real-time market",
    body: "Prices, the order book and your fills stream to the browser over WebSockets, and the client re-syncs by itself if it misses a message.",
  },
] as const;

const STEPS = [
  ["Connect", "Connect a wallet and sign in with a free message."],
  ["Produce", "A simulated meter reports surplus; it is minted as WATT."],
  ["Trade", "Sign orders in your wallet; the matcher pairs them at the reference price."],
  ["Settle", "The exchange contract settles both legs in a single transaction."],
] as const;

export default function Home(): React.JSX.Element {
  return (
    <div className="space-y-12 sm:space-y-16">
      <section className="grid items-center gap-8 pt-4 lg:grid-cols-[1.2fr_1fr]">
        <div className="space-y-5">
          <p className="text-primary text-sm font-medium">Peer-to-peer solar energy marketplace</p>
          <h1 className="text-3xl font-semibold tracking-tight text-balance sm:text-4xl">
            Sell your surplus solar. Buy local energy. Settled atomically.
          </h1>
          <p className="text-muted-foreground max-w-xl text-base text-pretty">
            WattSwap tokenizes surplus rooftop generation and lets prosumers and consumers trade it
            at a price that follows the weather. This is a testnet demo of the whole stack.
          </p>
          <div className="flex flex-wrap gap-3">
            <Link href="/dashboard" className={buttonVariants({ size: "lg" })}>
              Open dashboard <ArrowRight aria-hidden />
            </Link>
          </div>
        </div>
        <LandingPrice />
      </section>

      <section aria-labelledby="props-title" className="space-y-4">
        <h2 id="props-title" className="text-xl font-semibold">
          What makes it work
        </h2>
        <div className="grid gap-4 sm:grid-cols-2">
          {PROPERTIES.map(({ icon: Icon, title, body }) => (
            <Card key={title}>
              <CardBody className="space-y-2 p-5">
                <Icon aria-hidden className="text-primary size-5" />
                <h3 className="font-medium">{title}</h3>
                <p className="text-muted-foreground text-sm">{body}</p>
              </CardBody>
            </Card>
          ))}
        </div>
      </section>

      <section aria-labelledby="how-title" className="space-y-4">
        <h2 id="how-title" className="text-xl font-semibold">
          How it works
        </h2>
        <ol className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
          {STEPS.map(([title, body], i) => (
            <li key={title} className="bg-surface-1 rounded-xl border p-4">
              <span className="text-muted-foreground font-mono text-xs">0{i + 1}</span>
              <p className="mt-1 font-medium">{title}</p>
              <p className="text-muted-foreground mt-1 text-sm">{body}</p>
            </li>
          ))}
        </ol>
      </section>

      <section
        aria-labelledby="scope-title"
        className="bg-warning/10 border-warning/30 rounded-xl border p-5"
      >
        <h2 id="scope-title" className="text-lg font-semibold">
          What this is, honestly
        </h2>
        <ul className="text-muted-foreground mt-2 list-disc space-y-1 pl-5 text-sm">
          <li>It runs on a testnet. Smart meters are simulated by a labeled meter-simulator.</li>
          <li>Payments use mUSDC, a mock stablecoin with no monetary value.</li>
          <li>No physical energy is delivered and no real money moves.</li>
        </ul>
      </section>
    </div>
  );
}
