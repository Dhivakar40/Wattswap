# ADR-007: Pricing engine design

Status: accepted · Date: 2026-10-18

## Context

Phase 3 adds a price-discovery engine (CLAUDE.md §5.2). It must be testable to the last digit, safe to sign and
send on-chain, honest about the data it trusts, and usable in an accelerated demo. The contracts already exist
(Phase 1) and constrain what the engine can do.

## Decisions

1. **Pure core, edges for I/O.** `engine.step(state, inputs, params, now)` is a deterministic function of its
   arguments; weather, the database, Redis and the chain are adapters around it. Arithmetic is `Decimal` in a fixed
   context (28 digits, round-half-even); the only monetary output is an **integer** in mUSDC base units per kWh, the
   unit the contracts use. `PriceQuote` rejects anything that is not a plain `int`; the parameter API rejects JSON
   floats. The backtest runs the same `step`.

2. **Supply is generation plus tradeable inventory**, with a floor. `S = w_gen·Gen + w_inv·I`,
   `S_eff = max(S, S_min)`. At night the price is therefore inventory-driven rather than pinned to the ceiling.
   Until the order book exists, inventory is the WATT held by the owners of active meters (read from the chain);
   afterwards it is open sell volume.

3. **The breaker evaluates the raw price and only freezes on stale weather.** A raw move above 15 % in 10 minutes is
   an *anomaly*: the price is still repriced, the published band hint widens, and an event is stored. Stale weather
   (30 min) freezes the applied price with hysteresis. Rationale: a real price shock is information, not a fault;
   stale inputs are a fault.

4. **The limiter and the breaker must agree, and that is enforced.** The 0.25 %/tick limiter can move the applied price
   at most 10.5 % up / 9.5 % down in the breaker's 10-minute window, below the 15 % trigger. `cross_check()` rejects
   any parameter set (and the service refuses to boot at a tick rate) that violates it.

5. **Markets are regions; weather is per grid cell, not per meter.** A market is a bounding box (default: global).
   Meters map to 0.5° cells with integer math; one batched Open-Meteo request per poll covers all cells, so the
   request rate is independent of meter count (≈ 6/hour). Weather is untrusted: validated, rate-limit aware, with
   last-good fallback whose real age drives the breaker.

6. **Two clocks.** The market clock selects weather and the local-solar-hour load profile and may follow the
   simulator's accelerated time (`follow_readings`). The chain clock stamps quotes and oracle updates and is never
   ahead of the latest block, because the exchange rejects quotes from the future and older than `maxStaleness`.

7. **The exchange, not the oracle, verifies quotes.** `PriceOracle` stores the price and defines staleness and who may
   sign (`PRICER_ROLE`); `WattSwapExchange._checkQuote` recovers the signature. Tests therefore verify against the
   exchange (digest via `hashQuote`, role via the oracle, and a real `settle()`).

8. **`PriceOracle` is single-price, so only the primary market is pushed on-chain.** Quotes are signed per market, but
   the oracle holds one `(price, timestamp)`. A per-market oracle is a contract change and is out of scope; the
   `markets.is_primary` flag selects which market the oracle follows. Pushes happen on a 0.5 % move or a 120 s
   heartbeat (rate-limited to one per 30 s), with backoff on failure that never blocks ticking.

9. **The band is a capped hint.** The on-chain `bandBps` is an admin setting; the engine cannot change it. While
   anomalous or frozen it publishes a wider `band_bps` hint (×3), never above the on-chain value, for the Phase 4
   matcher.

10. **One engine, three ways to run it.** It runs inside the API process by default, behind a Redis leader lock so any
    number of instances is safe; `PRICING_EMBEDDED=false` plus `python -m app.pricing.run` (or the compose
    `pricing` profile) moves it to its own process. State is restored from the persisted ticks after a restart.

11. **Demand is proportional to the installed base by default.** This keeps the model self-consistent with no consumer
    registry, with a consequence that is documented rather than hidden: with no inventory, adding meters raises supply
    but not the raw price. `base_demand_kw` adds demand that does not scale with PV when that is wanted.

## Consequences

- Every formula has a hand-computed test; the day trace in `docs/pricing-model.md` is regenerated from the code and
  a test fails if they diverge.
- Prices are reproducible and auditable: each tick stores the full drivers breakdown.
- Frozen quotes keep a fresh timestamp so trading continues at the held price; `halt_quotes_on_freeze` is the opt-in
  way to stop trading instead.
- A real, unattended deployment needs the pricer key in a KMS/HSM and an Open-Meteo plan that fits its cell count.
