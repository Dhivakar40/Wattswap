# ADR-002: Order fill accounting without a nonce bitmap

Status: accepted · Date: 2026-10-18

## Context

CLAUDE.md §5.1 mentions a "nonce/filled-amount bitmap". A bitmap (Permit2-style) records one bit per
nonce and suits all-or-nothing orders. WattSwap orders support **partial fills**, which needs an amount per order.

## Decision

Track, per exchange:

- `filled[orderHash]` – cumulative filled Wh, with `fill <= amountWh - filled` enforced (`Overfill`).
- `cancelled[orderHash]` – single-order cancellation.
- `minNonce[maker]` – `cancelUpTo(n)` voids all orders with `nonce < n`; strictly increasing.

`orderHash` is the EIP-712 digest, so it commits to every field including `marketId` and `salt`.

## Consequences

- Same guarantees as a bitmap (no replay, no overfill, bulk cancel) with simpler, auditable code.
- One storage slot per *touched* order (about 22k gas first write) instead of shared bitmap words. Acceptable
  for a testnet-scale market; a bitmap optimisation can be added later without changing the signed type.
- The `nonce` field is *not* a uniqueness key: two live orders may share a nonce (`salt` differentiates them).
  Clients that want strict ordering should use increasing nonces.
