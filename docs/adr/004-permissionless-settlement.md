# ADR-004: Relayer-only settlement in v1; path to permissionless

Status: accepted (v1) · Date: 2026-10-18

## Context

CLAUDE.md §5.1 restricts `settle()` to `RELAYER_ROLE` in v1 and requires an ADR describing how to make it
permissionless later.

## Decision (v1)

Only `RELAYER_ROLE` may call `settle`. The relayer decides *which* crossing pair to settle and *when*, but it cannot:

- move funds beyond what two makers signed (limits, amount, expiry, market),
- trade outside the quote band, with a stale quote, or without a quote signed by a `PRICER_ROLE` holder,
- fill an order twice or beyond its size.

Residual relayer powers: choosing among valid matches (priority/ordering), delaying or censoring, and picking any
`execPrice` within `[sell.limit, buy.limit] ∩ quote ± band`. Within that range the relayer can favour one side by up
to the band (default 5%). This is the accepted v1 trust assumption; the matcher's rule (reference price clamped
into limits) is documented in the pricing/matching docs and observable via `TradeSettled`.

## Path to permissionless

1. **Remove `onlyRole(RELAYER_ROLE)`** from `settle` (no other contract change needed: everything is already
   signature-gated).
2. **Fix the execution price on-chain** instead of accepting it from the caller: `execPrice = clamp(quote.price,
   sell.limit, buy.limit)`. This removes the band as a relayer degree of freedom and makes any caller's output
   identical, so there is nothing to front-run for price.
3. **Relayer incentive / MEV hygiene:** pay the caller a small fixed share of the protocol fee (`tx.origin`-free;
   pay `msg.sender`), and enforce a per-pair `deadline` in the signed order or quote to bound griefing.
4. **Quote replay window:** consider adding a quote nonce / sequence to prevent settling against an older (still
   fresh) quote after a newer one exists.
5. **Griefing:** a permissionless caller can settle orders that are expensive to fail (e.g. a maker removing
   approval); mitigated by the fact that failures revert before any state change, and the caller pays gas.
6. Re-run invariant and fuzz suites with an adversarial caller added to the handler; update the threat model.

## Consequences

Keeping the role in v1 bounds front-running and lets the matcher enforce price-time priority off-chain. The
migration above is additive and does not change any signed EIP-712 type.
