# ADR-008: Trading architecture (order book, matcher, relayer, listener)

Status: accepted (Phase 4) · Related: ADR-002 (fill accounting), ADR-004 (permissionless settlement)

## Context

`WattSwapExchange.settle()` settles one signed buy against one signed sell, atomically, and only
for `RELAYER_ROLE`. Everything before that (accepting orders, price-time priority, funding, choosing
a price, getting a transaction mined with a still-valid quote) and everything after (knowing what
really happened on-chain) is off-chain and must be correct under crashes, races and reorgs.

## Decisions

1. **Reservations, not locks on the chain.** Each order has `filled_wh` (final on-chain fills) and
   `reserved_wh` (Wh in `matched`/`submitted` settlements). A DB CHECK `filled + reserved <= amount`
   makes over-allocation impossible regardless of which process misbehaves.
2. **Single writer by Redis lock, enforced twice more.** The matcher holds a Redis leader lock; each
   cycle also locks the market's orders `FOR UPDATE` (Postgres serialises a second writer) and
   re-checks lock ownership just before commit (a paused leader rolls back).
3. **Only the listener marks `confirmed`.** The relayer submits and detects failures; the listener
   applies `TradeSettled` at confirmation depth. One writer of final state means DB and chain cannot
   disagree about what happened. Fills and cancels are reversible (`chain_events.effects`) so a reorg
   is rolled back exactly; a periodic reconcile at the cursor block repairs any drift toward the chain.
4. **The matcher's price is provisional.** The relayer re-derives the execution price from a fresh
   quote immediately before signing, and releases the pair (with exponential cooldown) if no price
   satisfies the limits and the quote band. A quote must have `QUOTE_SAFETY_MARGIN` seconds of
   validity left when signed, and a stuck transaction is replaced at the same nonce with a higher fee
   and a fresh quote, so a quote cannot go stale in the mempool. A `StaleQuote` revert at mining is
   treated as transient (re-queue), not as the orders' fault.
5. **Band feasibility is part of matching.** The exchange also bounds `execPrice` to the signed quote
   ± `bandBps`; clamping into the limits can land outside it. Matcher and relayer intersect the
   limits with 90 % of the on-chain band, so a crossed pair that would revert is simply not matched.
6. **Funding is a pinned-block snapshot.** Balance, allowance and every `exchange.filled(hash)` are
   read at one block; commitments use the chain's `filled`, not the DB's. Otherwise the window
   between "mined" and "confirmed by the listener" double-counts a fill and cancels healthy orders.
   Over-committed makers lose their newest orders first (older orders keep time priority).
7. **Cancel is two things.** `DELETE /orders/{hash}` is a soft, server-side cancel: it never touches
   reserved Wh (the unreserved remainder leaves the book, the order is `cancel_pending` and is
   finalized when the in-flight settlements resolve) and is reported as `cancel_type: "offchain"`.
   On-chain `cancel`/`cancelUpTo` are binding and are reported as `"onchain"`. A cancel race (order
   voided on-chain while matched/submitted) fails the settlement, removes that order, frees the
   counterparty, and is **never retried**.
8. **Signature acceptance mirrors OpenZeppelin.** 65 bytes, `v` in {27, 28}, low `s`; EIP-2098
   compact signatures are rejected in v1. EIP-1271 makers are verified with `isValidSignature`.
9. **No contract changes.** Min order size, dust and funding rules are server-side; the contract's
   reverts remain the backstop and are classified (`app/chain/settle_errors.py`).

## Consequences

- Correctness of the off-chain system rests on a few invariants, each enforced in the database and
  tested against a real Anvil chain (concurrent matchers, forced lock loss, relayer crash between
  signing and broadcast, a reorg rolled back and re-applied).
- Off-chain cancel is only safe while settlement is relayer-gated (ADR-004 addendum).
- `listener_confirmations` trades latency for finality (1 on Anvil, 6 on Sepolia).
- A cancel-pending order keeps committing funds for its in-flight part only.
