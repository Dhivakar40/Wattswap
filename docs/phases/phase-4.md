# Phase 4 — Order book, matcher, settlement relayer, chain listener

Branch: `phase-4-trading` · Target tag: `v0.4.0` (after approval)

Status: **APPROVED — implementing.** Items marked **DECISION** were proposed defaults; the owner's changes are recorded under *Owner changes* at the end.

## Starting state (facts that shape the design)

- Phases 0–3 are merged (`v0.3.0` pending tag per the git log). No contract changes are planned for this phase.
- `WattSwapExchange.settle(buy, buySig, sell, sellSig, fill, execPrice, quote, quoteSig)` is `RELAYER_ROLE`-only and checks, in this order: side/market/enabled/**self-trade**/zero-fill/`sell.limit ≤ execPrice ≤ buy.limit`; per order: `expiry`, `cancelled[hash]`, `nonce ≥ minNonce[maker]`, `SignatureChecker` (EOA or EIP-1271, OZ `ECDSA` so **high-s and non-65-byte-or-64-byte signatures are rejected**), `fill ≤ amountWh − filled[hash]`; then the quote (`marketId`, `timestamp ≤ block.timestamp`, `block.timestamp − timestamp ≤ oracle.maxStaleness()`, signer has `PRICER_ROLE`, `|exec − quote| × 10000 ≤ quote × bandBps`); then `cost = fill × exec / 1000` (**`DustFill` only when `cost == 0`**), `fee = cost × feeBps / 10000`, and three `transferFrom`s. The buyer is debited `cost` in total (`cost − fee` to the seller, `fee` to the treasury).
- Nonces are a **floor** (`cancelUpTo`), not a bitmap. An order is individually voidable only via `cancel(order)` (`OrderCancelled`) or the floor (`NonceFloorRaised`). `filled[hash]` on-chain is the source of truth for partial fills.
- Order and quote EIP-712 vectors already exist from Phase 1 (`packages/shared/vectors/eip712.json`: `order_buy`, `order_sell`, `price_quote*`, signed with the public test keys, chain 31337, fixed contract addresses). `contracts/test/Vectors.t.sol` regenerates them and **fails if the committed file drifts**. Only the Python consumer is missing.
- Reusable from earlier phases: `TxSender` (Redis nonce lock, fee estimation, `simulate` as dry-run, raw tx persisted before broadcast via `on_signed`, `wait` over several hashes, `revert_reason` replay), `RevertDecoder`, the mint worker's leader-lock loop and crash-recovery pattern, `ratelimit.hit`, SIWE auth deps, the Phase 3 `price:{market}:latest` / `quote:{market}:latest` Redis keys and envelope format, and the engine's `open_order_wh` / `recent_fill_wh` / `_inventory` hooks (`pricing_inventory_source = auto` already switches to open sell volume once any exists).
- Schema stubs `orders`, `trades`, `settlements` exist and are empty; migration `0003` reshapes them.

## Scope

In: server-side EIP-712 order verification (byte-exact with the contract), funding checks and over-commitment control, the order book (Postgres, Redis hot reads), price-time matcher under a Redis single-writer lock, the relayer (nonce management, retries, fee bumping, quote freshness at mining time), the idempotent reorg-aware chain listener, cancel handling (off-chain and on-chain races), engine feeds, REST trading API, rate limiting, minimum sizes, metrics, docs, tests, CI.
Out: WebSocket gateway (Phase 5), UI (Phase 6). **Publish-only exception:** matcher/listener publish `orderbook:{market}`, `trades:{market}` and `user:{address}` envelopes to Redis in the Phase 3 format (a few lines each) so Phase 5 only forwards them. No consumer is added here.

## Architecture

```
POST /orders ──► verify (pure: hash, low-s sig, bounds) ──► funding check (chain, pinned block) ──► orders (Postgres) ──► Redis book cache
                                                                                                         │
                         ┌───────────────────────────────────────────────────────────────────────────────┘
                         ▼
 Matcher  (Redis leader lock + DB row locks)  price-time priority, skips self-trade
   │ reads quote:{market}:latest, on-chain bandBps          creates trade + settlement(matched), reserves Wh on both orders
   ▼
 Relayer  (Redis leader lock)   pre-flight (funding, cancel state, expiry) ─► FRESH quote ─► recompute exec price ─► simulate ─► sign ─► persist raw tx ─► broadcast
   │                              settlement: matched → submitted ;  bump / re-quote when stuck ;  revert → classify
   ▼
 WattSwapExchange.settle()  ──events──►  Listener (Redis leader lock)  confirmation depth N, block-hash chain, reorg rollback, idempotent (txHash, logIndex)
                                              │ applies TradeSettled / OrderCancelled / NonceFloorRaised / Transfer
                                              ▼  settlement: submitted → confirmed ; orders filled_wh ; trades tape ; Redis events
 Funding sweeper (leader lock): re-checks every maker with open orders, auto-cancels unfunded (newest first)
```

Modules (all under `services/api/app/`): `chain/orders.py` (pure EIP-712 order/quote hashing and signature checks), `matching/{book,matcher,funding,rules}.py`, `chain/settlement.py` + `workers/relayer.py`, `workers/listener.py`, `workers/sweeper.py`, `routers/{orders,trades}.py`, `schemas/trading.py`, `trading/run.py` (standalone runner, like `pricing/run.py`).

### Order state

`orders.status`: `open → partially_filled → filled`, or `cancelled` / `expired` (with `cancel_reason`).
Amounts per order: `amount_wh`; `filled_wh` (confirmed on-chain fills only); `reserved_wh` (Wh in trades that are `matched` or `submitted`). **Invariant, enforced by a DB CHECK:** `filled_wh + reserved_wh ≤ amount_wh`. Matchable remainder = `amount − filled − reserved`; funding commitment = `amount − filled` (reserved Wh is still unspent until confirmed).
Settlement states: `matched → submitted → confirmed | failed` (CLAUDE.md §5.3). A failed settlement releases `reserved_wh` back and stores a reason.

## 1. EIP-712 order verification (must match the contract exactly)

- `chain/orders.py` builds the `Order` typed data with the domain `{name: "WattSwapExchange", version: "1", chainId, verifyingContract}` and computes `struct_hash`, `domain_separator`, `digest` (= `exchange.hashOrder`). The digest is the order's identity (`orders.order_hash`). Two independent implementations inside the Python code are cross-checked in tests: `eth_account.encode_typed_data` and a hand-rolled `keccak(0x1901 ‖ domainSep ‖ structHash)` with `eth_abi.encode`.
- **Signature rules mirror OZ `ECDSA.tryRecover` so that nothing accepted off-chain reverts on-chain**: length 65 (64-byte EIP-2098 compact signatures are rejected with a clear error — normalisable but a v1 non-goal), `v ∈ {27, 28}`, `s ≤ secp256k1n/2` (high-s rejected; this is the classic malleability trap), recovered signer must equal `maker`. For makers with contract code (EIP-1271) the server cannot recover; it calls `isValidSignature(digest, sig)` via `eth_call` (same as `SignatureChecker`) and re-does it in the relayer pre-flight.
- Field validation: `maker` checksum/lowercase normalisation and equal to the SIWE session address; `side ∈ {0,1}`; `marketId` known + enabled (DB) and `exchange.marketEnabled` (cached 30 s); all uint256 fields in range; `nonce ≥ exchange.minNonce(maker)`; `expiry` between `now + min_order_life` (60 s) and `now + max_order_life` (7 d); `amountWh ≥ min_order_wh`; `limitPrice` within `[min_limit_price, max_limit_price]` (sanity bounds; guards `uint256` overflow in `fill × price`); order not already cancelled or filled on-chain (`cancelled`, `filled`).
- Idempotent: posting the same signed order twice returns the stored order (HTTP 200), never a duplicate. A different order that reuses a salt is a different hash and is accepted.

**Cross-language vector gate (CI):**
1. `Vectors.t.sol` gains order-specific edge vectors: all-FF `salt`, `side` 0 and 1, `nonce` 0 and a large value, `expiry` large, `amountWh`/`limitPrice` near `uint256` max (encoded as **decimal strings** in the JSON because JSON numbers lose precision above 2^53; the existing small vectors stay numeric and both forms are accepted by the loader), a maker address with leading zero bytes, and a second market id. The JSON is regenerated with `WRITE_VECTORS=1`; the forge test still fails on drift.
2. `tests/unit/test_order_vectors.py` reproduces **every** order and quote vector in the file: struct hash, domain separator, digest, recovered signer, and the **byte-identical signature** (RFC 6979 deterministic signing with the published test keys). It parametrises over whatever vectors exist, so a new vector can never be silently skipped, and it asserts the vector count and names so deleting one fails the test.
3. CI: the forge job runs `forge test` (drift check) and then `git diff --exit-code packages/shared/vectors`; the pytest unit job runs the Python test unconditionally (not behind the integration marker, no chain required); the TS shared vector test is updated to read string-or-number uints and keeps passing.
4. Anvil integration: for hypothesis-generated orders, `exchange.hashOrder(order) == python digest` and a Python-signed order is accepted by a real `settle()`.

## 2. Funding, balances, allowances and over-commitment

Per maker, per token, **total commitment across all open orders** must fit within **both** balance and allowance:

| Side | Token | Committed amount | Must be ≤ |
|---|---|---|---|
| sell | WATT | Σ (`amount − filled`) of the maker's open sells | `WATT.balanceOf(maker)` **and** `WATT.allowance(maker, exchange)` |
| buy | mUSDC | Σ `floor((amount − filled) × limitPrice / 1000)` of the maker's open buys (worst case: exec price can be at most the limit; fee comes out of that same debit) | `mUSDC.balanceOf(maker)` **and** `mUSDC.allowance(maker, exchange)` |

- **At acceptance:** the new order is added to the maker's existing commitment; if it does not fit, 422 `insufficient_balance` or `insufficient_allowance` with the numbers (`required`, `balance`, `allowance`, `committed`). Nothing is stored.
- **Consistent snapshot (important):** balance, allowance and every relevant `exchange.filled(hash)` are read **at one pinned block number**, and commitments use `amount − filled_chain_at_that_block` rather than the DB's `filled_wh`. Otherwise, between a settle being mined and the listener confirming it, the balance has already dropped while the DB still counts the filled amount, and a healthy order would be cancelled as "unfunded". The pinned-block read removes that double count.
- **Re-check right before settlement:** the relayer pre-flight re-evaluates both makers with the same function, additionally subtracting Wh/cost reserved by *other* in-flight settlements of the same maker, and re-reads `cancelled`, `minNonce`, `filled` and expiry (must still be valid at `chain_time + expiry_margin`, default 90 s, so it does not expire while mining).
- **Auto-cancel unfunded orders:** (a) pre-flight failure cancels the order that is short; (b) the **funding sweeper** (every `FUNDING_SWEEP_SECONDS`, default 20 s; plus triggered by observed `Transfer`/`Approval`-relevant events from the listener for makers with open orders) re-checks every maker with open orders and, when a maker is over-committed, cancels **newest orders first** until the remainder fits, so earlier orders keep their time priority. Reason is stored as `unfunded:balance` or `unfunded:allowance`. The other leg of any matched, unsubmitted trade is released back to the book.
- The matcher also refuses to match an order whose maker was found unfunded within the last sweep interval (cheap Redis flag `unfunded:{maker}` set by the sweeper/pre-flight).

## 3. Order book and matcher

- Source of truth: Postgres (`orders`, index `ix_orders_book` already covers market/side/status/limit/created_at). Redis keeps a hot-read cache of the aggregated depth (`book:{market}` as price levels) rebuilt after every match cycle and every accepted/cancelled order, used by `GET /book`. Cache loss is harmless (rebuilt on miss).
- **Price-time priority:** bids by `limit_price DESC, created_at ASC, id ASC`; asks by `limit_price ASC, created_at ASC, id ASC`. A bid and ask cross when `bid.limit ≥ ask.limit`.
- **Self-trade prevention:** for the best bid the matcher walks the ask side in priority order and **skips** asks from the same `maker` (case-normalised); it does not skip the bid. If the maker's own ask is the best price, the next-best counterparty is matched instead, and a maker's own orders simply rest. The contract's `SelfTrade` revert is therefore never reached by the matcher (it remains the backstop, and a unit test proves the matcher never produces such a pair).
- **Execution price:** `exec = clamp(reference, sell.limit, buy.limit)` where `reference` is the latest signed engine price (`quote:{market}:latest`). **Band feasibility (found while reading the contract):** the exchange also requires `|exec − quote.price| ≤ bandBps` of the quote. When the limits are far from the quote, the clamp lands outside the band and `settle()` would revert with `PriceOutsideBand`. The matcher therefore computes the intersection `[sell.limit, buy.limit] ∩ [q(1−b), q(1+b)]` with `b = on-chain bandBps × 0.9` (a 10 % safety margin for quote drift) and only matches when it is non-empty; exec is the clamp of the reference into that intersection. Pairs that cross but are band-infeasible stay on the book (a metric counts them).
- **No matching when the price is unsafe:** missing quote, quote older than `maxStaleness − quote_safety_margin`, breaker frozen with `halt_quotes_on_breaker`, or admin "matching paused" → the matcher idles and reports why (metric + admin state endpoint). It never invents a price.
- **Minimum fill / dust:** `fill = min(bid_rem, ask_rem)`. A fill is only produced if, using the contract's exact integer math, `cost = fill × exec // 1000 ≥ max(1, min_fill_cost)` (default `MIN_FILL_COST = 1000` µmUSDC = 0.001 mUSDC) **and** `fill ≥ min(min_fill_wh, remaining of the smaller side)` (default `MIN_FILL_WH = 100`). Remainders `0 < rem < min_fill_wh` after a partial fill cannot trade again; the order is closed as `cancelled` with reason `dust_remainder` (not `filled`, so history stays honest). Orders under `MIN_ORDER_WH` (default **100 Wh = 0.1 kWh**) are rejected at acceptance. This guarantees the relayer never spends gas on a `DustFill`.
- **Single writer:** the loop holds a Redis lock (`lock:matcher`, TTL 30 s, reacquired every cycle, same pattern as the mint worker). **Defence in depth against a lost lock / GC pause / clock skew** (two writers for a moment): every match cycle runs in one DB transaction that (1) `SELECT … FOR UPDATE` the candidate orders in a deterministic id order, (2) re-checks that the lock token is still owned just before commit and rolls back otherwise, and (3) relies on the `filled_wh + reserved_wh ≤ amount_wh` CHECK so a second writer *cannot* over-reserve even if everything else fails. A trade row has a unique `(buy_order_id, sell_order_id, seq)`-style idempotency key so the same match cannot be created twice.
- Each match creates `trades` (provisional exec price, cost, fee computed with the contract's math) and `settlements(status=matched)`, increments `reserved_wh` on both orders, then publishes events.
- Cycle trigger: a short poll (default 500 ms) plus an immediate nudge via Redis pub/sub when an order is accepted (so latency is not poll-bound).
- Expiry: orders past `expiry` become `expired` (swept by the matcher cycle) and never match. Their `reserved_wh` is not touched: reserved Wh belongs to the settlement.

## 4. Relayer

- Takes `matched` settlements oldest-first (skipping those in cooldown) and runs this pipeline, **all inside a Redis leader lock** (`lock:relayer`) plus the existing `lock:tx:relayer` nonce lock inside `TxSender`:
  1. **Pre-flight** (section 2 re-check; cancel state; expiry). A short order is cancelled with a reason; the counterpart is released; the settlement is `failed` with reason `preflight:<why>` and **no gas is spent**.
  2. **Fresh quote immediately before signing:** read the pricing engine's latest signed quote (`quote:{market}:latest`); require `age ≤ maxStaleness − QUOTE_SAFETY_MARGIN` (default margin **60 s** on a 300 s staleness window, i.e. refuse quotes older than 240 s; configurable and validated `0 < margin < maxStaleness`). If the quote is too old, ask the pricing service for a fresh tick-sign (the engine signs a new quote every tick, 15 s), wait up to a bounded time, then defer (not fail).
  3. **Re-derive exec price** from that fresh quote within the same band-feasibility rule as the matcher (the matched price was provisional; it is final only when signed). If no feasible price exists any more the settlement is returned to the book (`released:price_band`), not failed.
  4. `TxSender.simulate` (gas estimate = dry run on the latest state). Terminal contract rejections are classified (table below) before any nonce is used.
  5. Sign, **persist the raw signed tx and hash on the settlement before broadcast** (`on_signed`), broadcast, `status = submitted`.
- **Quote freshness at mining time, not only submit time:** the tx may sit in the mempool. The relayer tracks `quote_timestamp` per submitted settlement. If the tx is unmined after `min(bump_after, maxStaleness − quote_safety_margin − quote_age)` seconds (default `bump_after` 20 s), it sends a **replacement at the same nonce with a fee bump (×1.25, which also satisfies the node's ≥10 % rule) and a freshly fetched quote**, recomputing the price. All hashes for the nonce are watched; whichever mines is the result. Because replacements are bounded by `maxStaleness − margin`, a tx carrying a quote that could be stale at inclusion is always superseded first. If a mined settle still reverts with `StaleQuote`/`QuoteFromFuture`/`PriceOutsideBand` (chain-time jump), it is treated as **transient**: re-queued for a new quote, counted against `SETTLE_MAX_ATTEMPTS` (default 5), not failed immediately.
- **Nonce management:** `TxSender` gives `pending`-count nonces under the Redis lock; the relayer allows up to `SETTLE_MAX_IN_FLIGHT` (default 4) concurrent submitted settlements, one nonce each. A "nonce too low" node error retries with a fresh nonce; the persisted-raw-tx recovery rebroadcasts the *same* signed tx after a crash (never re-signs with a new nonce) — identical to the proven mint-worker design.
- **Confirmation:** the relayer waits for `SETTLE_CONFIRMATIONS` (default 1 on Anvil, 3 on Sepolia) only to detect reverts and stuck transactions. A **successful** receipt does *not* move the settlement to `confirmed`; the listener does, from the `TradeSettled` event at confirmation depth, so there is a single writer of "confirmed" and DB state cannot diverge from the chain log. A reverted receipt: replay with `revert_reason`, classify, act.

### Revert classification (decoded with `RevertDecoder`; MockUSDC/EnergyToken ABIs added so OZ `ERC20Insufficient*` errors decode)

| Revert | Meaning | Action |
|---|---|---|
| `OrderIsCancelled(hash)`, `NonceTooLow` | **Cancel race**: the maker cancelled on-chain after matching | Settlement → `failed` (`cancel_race`), that order → `cancelled` (`on_chain_cancel`), `reserved_wh` released on both, counterpart back on the book. **Never retried.** |
| `OrderExpired`, `InvalidOrderSignature`, `Overfill` | Order unusable | That order removed with reason; counterpart released; settlement `failed` |
| `ERC20InsufficientBalance(sender, …)` / `ERC20InsufficientAllowance(spender, …)` | Maker underfunded | Relayer re-reads both makers on-chain to attribute it (allowance errors name the exchange, not the maker), cancels the short order(s) `unfunded:*`, releases the other |
| `StaleQuote`, `QuoteFromFuture`, `PriceOutsideBand`, `InvalidQuoteSigner` | Quote/clock problem, not the orders' fault | Transient: re-quote and retry (bounded). `InvalidQuoteSigner` additionally raises an alert (pricer key not role-granted) |
| `EnforcedPause`, `AccessControlUnauthorizedAccount`, `MarketDisabled` | Systemic | Settlement returns to `matched`, relayer pauses itself with an alarm; no order is blamed |
| `SelfTrade`, `PriceOutOfLimits`, `InvalidSide`, `MarketMismatch`, `DustFill`, `ZeroFill` | Matcher bug | Settlement `failed`, both orders released, **error log + `wattswap_matcher_invariant_violations_total`** |
| RPC/timeout/underpriced/nonce errors | Infrastructure | Backoff + retry, no state change |

On any `failed` settlement the **orders return to the book with the reason recorded** (`settlements.failure_reason`, `orders.cancel_reason` where an order is removed), and the pair gets a Redis cooldown (`PAIR_COOLDOWN_SECONDS`, default 30 s) so a persistent problem cannot become a hot loop.

## 5. Chain listener (idempotent, reorg-aware)

- Tails the exchange for `TradeSettled`, `OrderCancelled`, `NonceFloorRaised`, `MarketUpdated`, `FeeUpdated`/`BandUpdated` (cache invalidation), and `Transfer` on WATT and mUSDC (the latter two feed balance-cache invalidation and the funding sweeper, and the activity history in Phase 6). `eth_getLogs` windows of `LISTENER_BATCH_BLOCKS` (default 500).
- **Idempotency:** every log is stored in `chain_events` keyed `UNIQUE(tx_hash, log_index)` (plus `block_number`, `block_hash`, `address`, `name`, `args` JSONB, `applied` flag). Applying an event is `INSERT … ON CONFLICT DO NOTHING`, and the side effects run only on insert, inside the same DB transaction. Re-running the listener over the same range (restart, retry, two instances) is a no-op.
- **Finality/reorgs:** events are *applied* only at depth `LISTENER_CONFIRMATIONS` (default 2 on Anvil, 6 on Sepolia). The listener keeps the hash of every block it has applied for the last `LISTENER_REORG_WINDOW` (default 64) blocks in `indexer_blocks`. Each cycle it verifies the stored hash of the cursor block against the node; on a mismatch it walks back to the common ancestor and **rolls back** events above it: the `chain_events` rows are deleted, `TradeSettled` effects are reversed (settlement `confirmed → submitted`, `filled_wh −= fill`, `reserved_wh += fill`, trade's `tx_hash/log_index/block_number` cleared), `OrderCancelled` reversals restore the order if it was only cancelled by that event; then it re-reads the new canonical chain. A tx that does not reappear is picked up by the relayer's stuck-tx logic (re-broadcast of the persisted raw tx, then re-quote). Reorgs deeper than the window are treated as an operator alarm (metric + stop, no silent divergence).
- **Reconciliation backstop:** on start and every `RECONCILE_SECONDS` (default 120) the listener compares `exchange.filled(hash)` and `cancelled(hash)` for all open/partially filled orders (pinned block, batched) with the DB and repairs drift with a loud warning. The on-chain value always wins.
- **Effects of events:**
  - `TradeSettled(buyHash, sellHash, fill, execPrice, cost, fee)`: find the settlement by order hashes and the persisted tx hash(es); `confirmed`, set `trades.{tx_hash, log_index, block_number, exec_price, cost, fee, executed_at}` from the event (the chain's values override the provisional ones), `filled_wh += fill`, `reserved_wh −= fill`, order status update. If no matching settlement exists (settled by another relayer instance or restored from backup) it records the trade as `external`, applies fills to known orders, and logs a warning.
  - `OrderCancelled(maker, hash)` → order `cancelled` (`on_chain_cancel`); if it has `reserved_wh` in a `matched`/`submitted` settlement, that settlement is failed **now** (`cancel_race`), counterpart released. The relayer will not submit it.
  - `NonceFloorRaised(maker, newMin)` → every open order of the maker with `nonce < newMin` is cancelled (`nonce_floor`), same in-flight handling.
- **Cancel races, end to end:** race lost before broadcast → caught by pre-flight/`simulate` (no gas). Race lost after broadcast → the mined settle reverts with `OrderIsCancelled`/`NonceTooLow`, classified above → `failed` + order removed, **no retry**. Event arrives first → the in-flight settlement is failed from the listener side and the relayer's mined-revert path sees it is already `failed` and does nothing (idempotent).

## 6. REST API (`/api/v1`)

| Endpoint | Auth | Notes |
|---|---|---|
| `GET /markets/{id}/order-config` | public | what a client needs to build and sign: EIP-712 domain, types, exchange/token addresses, chain id, `minOrderWh`, `minFillWh`, limit-price bounds, `bandBps`, `feeBps`, expiry bounds |
| `POST /orders` | SIWE | body = the signed `Order` + `signature`; maker must equal the session address. Rate limited per address. 201 new / 200 duplicate. Errors use the existing envelope with specific codes (`bad_signature`, `insufficient_balance`, `insufficient_allowance`, `order_too_small`, `order_expired`, `nonce_too_low`, `market_disabled`, `rate_limited`, `too_many_open_orders`) |
| `GET /orders?status=&market=&limit=&before=` | SIWE | caller's orders with fills |
| `GET /orders/{hash}` | SIWE (owner) | order, its settlements and failure reasons |
| `DELETE /orders/{hash}` | SIWE (owner) | **off-chain cancel**: removes the order from the book immediately. Refused with 409 `in_flight` if part of it is matched/submitted (then it must be cancelled on-chain: see below) |
| `GET /markets/{id}/book?depth=` | public | aggregated price levels `{price, amountWh, orders}`, no addresses |
| `GET /markets/{id}/trades?limit=&before=` | public | confirmed trades only (the tape) |
| `GET /trades?mine=true` | SIWE | caller's trades, any state, with settlement status and tx hash/explorer-ready fields |
| `GET /admin/trading/state`, `POST /admin/trading/{pause,resume}` | admin | leader holders, queue depth by status, listener cursor and lag, relayer in-flight, last errors |

**DECISION: two kinds of cancel.** The API's `DELETE` is a *server-side* cancel: the signed order stays valid on-chain until it expires, which is a real property of off-chain order books. A maker who wants a guarantee (or whose order is in flight) calls `cancel(order)` / `cancelUpTo(nonce)` on the exchange; the listener reflects it. The docs and `order-config` say so plainly. (Making off-chain cancel binding would need maker-signed cancels checked on-chain: a contract change I'm not proposing.)

**Rate limiting and caps:** per-address placement limit `RATE_LIMIT_ORDERS` (default **30 per window of 60 s**) via Redis `hit`, keyed on the *authenticated address* (plus the existing per-IP limit), and a cap `MAX_OPEN_ORDERS_PER_MAKER` (default 50). Both fire before any chain call, so a spammer costs only a Redis increment.

## 7. Feeding the pricing engine

Most hooks exist from Phase 3; this phase makes the data correct and proves it:
- **Bid volume:** `open_order_wh(BUY)` = unfilled Wh of open, unexpired, **unflagged-unfunded** buys (`amount − filled`). Reserved Wh still counts until confirmed.
- **Inventory term:** with `pricing_inventory_source = auto`, the engine switches from held balances to **open sell volume** as soon as any funded sell is on the book (already coded; now fed by real orders). Source is exposed in tick drivers (`inventory_source`).
- **Fill velocity:** `recent_fill_wh` is corrected to count only **confirmed** settlements by `executed_at` (today it sums `trades.created_at`, which would count matched-but-failed trades).
- Test: place sells and bids, run ticks, observe the inventory source flip, the demand and price response, and fill velocity move after a confirmed settle. Any unfunded/cancelled order drops out on the next tick.

## 8. Data model (migration `0003`)

- `orders`: add `reserved_wh`, `unfunded_since`, `cancel_reason` stays; add CHECK `filled_wh + reserved_wh <= amount_wh`; indexes for maker (open orders) and expiry.
- `trades`: add `settlement_id` (FK), `status`-free (state lives on the settlement), `external bool`; `created_at` stays (match time); `executed_at` becomes the chain time.
- `settlements`: add `buy_order_id`, `sell_order_id`, `fill_wh`, `provisional_exec_price`, `exec_price`, `quote_price`, `quote_timestamp`, `raw_tx`, `tx_nonce`, `tx_hashes` (JSONB list, for replacements), `submitted_at`, `block_number`, `cooldown_until`, `next_attempt_at`.
- New: `chain_events` (UNIQUE `tx_hash, log_index`), `indexer_blocks` (`block_number`, `block_hash`), `indexer_cursor`.
- Alembic round-trip (upgrade, downgrade, upgrade) is already tested in `test_migrations.py` and is extended.

## 9. Configuration (`.env.example` documented)

`TRADING_ENABLED`, `TRADING_EMBEDDED`, `MIN_ORDER_WH=100`, `MIN_FILL_WH=100`, `MIN_FILL_COST=1000`, `MIN_LIMIT_PRICE`, `MAX_LIMIT_PRICE`, `MIN_ORDER_LIFE_SECONDS=60`, `MAX_ORDER_LIFE_SECONDS=604800`, `EXPIRY_MARGIN_SECONDS=90`, `MAX_OPEN_ORDERS_PER_MAKER=50`, `RATE_LIMIT_ORDERS=30`, `MATCH_POLL_MS=500`, `QUOTE_SAFETY_MARGIN_SECONDS=60`, `BAND_SAFETY_PCT=90`, `SETTLE_CONFIRMATIONS`, `SETTLE_MAX_IN_FLIGHT=4`, `SETTLE_MAX_ATTEMPTS=5`, `SETTLE_BUMP_AFTER_SECONDS=20`, `PAIR_COOLDOWN_SECONDS=30`, `FUNDING_SWEEP_SECONDS=20`, `LISTENER_CONFIRMATIONS`, `LISTENER_REORG_WINDOW=64`, `LISTENER_BATCH_BLOCKS=500`, `RECONCILE_SECONDS=120`. Startup validation (like the pricing cross-check): `0 < QUOTE_SAFETY_MARGIN < on-chain maxStaleness`, `BAND_SAFETY_PCT ∈ (0, 100]`, relayer key present and distinct (already enforced), relayer holds `RELAYER_ROLE`. Misconfiguration = refuses to start the trading workers, with the reason logged.

## 10. Metrics

`wattswap_orders_total{result}`, `wattswap_orders_open{side}`, `wattswap_match_cycle_seconds`, `wattswap_matches_total`, `wattswap_match_skipped_total{reason}` (self_trade, band_infeasible, stale_quote, no_quote, cooldown), `wattswap_settlements{status}`, `wattswap_settle_failures_total{reason}`, `wattswap_relayer_in_flight`, `wattswap_listener_lag_blocks`, `wattswap_listener_reorgs_total`, `wattswap_funding_cancels_total{reason}`, `wattswap_leader{role}`, `wattswap_matcher_invariant_violations_total`.

## Test plan

**Unit / pure (no services):**
- Order hashing and signature rules: every shared vector (struct hash, domain, digest, signer, **byte-identical signature**), vector-count guard, string-and-number uint forms, high-s rejected, wrong `v`, wrong length, wrong chain id / wrong verifying contract produce a different digest (replay protection across chains and deployments), hypothesis round-trip sign→recover.
- Matcher (pure core over in-memory books, hypothesis): price-time order, partial fills, never self-trade, never crosses limits, exec within limits and band, no fill with `cost == 0`, dust remainder closure, conservation of Wh, deterministic output.
- Funding maths: sums per maker, newest-first cancellation selection, ceil/floor behaviour matches the contract's integer math.
- Revert classification table (each decoded error → action).
- Listener apply/rollback as pure state transitions.

**API (Postgres + Redis, chain stubbed where the point is the API):** validation errors for every rejection code, idempotent POST, per-address rate limit (and that it is per address, not per IP), open-order cap, auth/ownership 401/403/404, off-chain cancel vs `in_flight` 409, book aggregation without addresses, tape shows confirmed only.

**Anvil integration (CI gate; real Postgres, Redis, Anvil, deployed contracts; runs in `-m integration` and must not skip in CI):**
1. **Happy path:** two funded wallets, crossing orders → matcher → relayer → on-chain `settle` → listener confirms → DB balances/fills/trades consistent with `balanceOf` and `exchange.filled`; fee to treasury; `TradeSettled` values equal the DB.
2. **Partial fills:** one big sell against two smaller buys; remainders rest; second fill later; dust remainder closed with `dust_remainder`; `filled + reserved ≤ amount` throughout.
3. **Cancel:** off-chain cancel removes from the book; on-chain `cancel` and `cancelUpTo` are reflected by the listener.
4. **Expired order:** never matches; swept to `expired`; an order that expires between match and submit is caught by pre-flight (no gas).
5. **Insufficient allowance / balance:** rejected at acceptance with the exact numbers; allowance revoked after acceptance → sweeper auto-cancels; revoked between match and submit → pre-flight cancel and counterpart released.
6. **Settle revert → orders returned to the book:** deterministic mined revert (automine off, revoke allowance after broadcast, mine) → `ERC20InsufficientAllowance` → settlement `failed`, short order `unfunded`, counterpart back on the book with a recorded reason and cooldown; and a matcher-bug-class revert path via an injected bad pair (invariant violation counted).
7. **Over-commitment:** same balance, many orders: the (n+1)th is rejected at acceptance; after the balance is spent elsewhere the sweeper cancels newest-first and older orders keep priority; allowance case separately; buy-side cost maths with a non-trivial price.
8. **Self-trade:** a maker's own crossing orders never match each other; a third party fills against the next-best order instead.
9. **Quote freshness at mining time:** (a) a stale quote is never signed (pre-sign age check); (b) automine off, tx stuck, chain time advanced → replacement at the same nonce with a fresh quote mines and succeeds; (c) a settle forced to revert with `StaleQuote` is re-queued and later succeeds, not failed.
10. **Cancel races:** on-chain `cancel` between match and broadcast (caught pre-flight, no gas) and between broadcast and mining (mined revert `OrderIsCancelled` → `failed`/`cancel_race`, order removed, **no retry**, counterpart back on the book); `cancelUpTo` variant; listener-first ordering variant.
11. **Single writer:** **two matcher instances (and two relayers, two listeners) run simultaneously** on a book of N crossing pairs: exactly one holds each lock, every pair is matched exactly once (no duplicate trades, no over-reservation), the total on-chain filled equals the expected sum; then kill the leader and prove the follower takes over after the TTL; and a forced lock loss test (delete the key mid-cycle) proves the DB transaction check + CHECK constraint still prevent a double reservation.
12. **Relayer crash recovery:** kill after persisting the raw tx but before broadcast → restart rebroadcasts the same tx, exactly one `TradeSettled`.
13. **Listener:** idempotent replay (run twice, same DB), restart mid-range, **reorg** using `evm_snapshot`/`evm_revert` (event applied then rolled back, DB returns to `submitted`, re-applied after re-mining), and the reconciliation repair.
14. **Rate limit & minimum size:** 31st order in a window gets 429; a below-minimum order is rejected; no relayer transaction is ever sent for a dust fill.
15. **Pricing feed:** inventory source flips to open sells, bid volume lifts demand, fill velocity updates only on confirmed trades; unfunded/cancelled orders drop out.
16. **Vectors on a real chain:** python digest == `exchange.hashOrder` for generated orders.

**Gates before push:** `make lint` (ruff + mypy strict), `make test` (pytest unit/api/integration), forge tests unchanged-green plus the new vectors (coverage stays 100 %), Slither unchanged, shared TS tests green, CI updated (vector drift check, integration job runs the trading suite with services, no skips), `docs/phases/phase-4.md` updated with outcome.

## Files (high level)

```
contracts/test/Vectors.t.sol                      + order edge-case vectors
packages/shared/vectors/eip712.json               regenerated;  packages/shared/src/vectors.test.ts  string-or-number uints
services/api/app/chain/{orders,settlement}.py     services/api/app/matching/{book,matcher,funding,rules}.py
services/api/app/workers/{relayer,listener,sweeper}.py   services/api/app/trading/run.py
services/api/app/routers/{orders,trades}.py   schemas/trading.py   models/tables.py   core/config.py   core/metrics.py
services/api/app/pricing/repo.py                  confirmed-fill velocity, funded-order volume
services/api/alembic/versions/0003_trading.py
services/api/tests/{unit,api,integration}/…       (incl. test_order_vectors.py, test_trading_*.py)
docs/settlement.md  docs/api.md  docs/threat-model.md  docs/adr/008-trading-architecture.md  README  .env.example  CI  Makefile
tools/trade_demo.py                                labeled demo script: two wallets cross orders (README walkthrough)
```
No new runtime dependency is expected (web3, eth-account, redis, SQLAlchemy already present); `hypothesis` is already a dev dependency.

## Risks

| Risk | Mitigation |
|---|---|
| Crossed but band-infeasible pairs (exec clamp outside quote band) reverting on-chain | Matcher and relayer both intersect limits with the band (90 % of on-chain `bandBps`); metric for skipped pairs |
| Funding check double-counting between mining and listener confirmation | Pinned-block reads of balance, allowance and `filled` (section 2) |
| Quote goes stale while a tx sits in the mempool | Freshness margin at signing, bounded replacement at the same nonce with a fresh quote, transient classification of `StaleQuote` |
| Lock lost mid-cycle gives two writers | DB row locks, ownership re-check before commit, CHECK constraint backstop; tested by deleting the lock mid-cycle |
| Off-chain cancel is not binding on-chain | Stated plainly; on-chain cancel path is first-class; in-flight orders refuse off-chain cancel |
| Reorg deeper than the window | Alarm + stop instead of silent divergence; reconcile against chain state |
| Spam and griefing (many unfunded orders, relayer gas burn) | Funding check at acceptance, per-address rate limit and open-order cap, pre-flight and dry-run before every send, min sizes, pair cooldown |
| Accepting a signature the contract will reject (high-s, EIP-2098) | Server enforces OZ's exact acceptance rules; vector tests cover them |
| Windows/Git Bash friction for Anvil time-travel tests | Same Anvil harness as Phase 2/3; `evm_*` RPCs used through the existing helpers |

## Decisions I'm proposing (override any with your "go")

1. **No contract changes.** Min size, dust, funding and band feasibility are enforced server-side; the contract's reverts are the backstop.
2. **Two cancel kinds** (server-side `DELETE`, binding on-chain `cancel`/`cancelUpTo`), as described.
3. **Exec price is final only at signing:** the matcher's price is provisional; the relayer recomputes it from the fresh quote, and releases the pair if no feasible price remains.
4. **Only the listener marks `confirmed`;** the relayer never does. One writer of final state.
5. **Funding uses pinned-block snapshots** and cancels newest-first so older orders keep time priority.
6. **Dust remainders are cancelled (`dust_remainder`), not marked filled.**
7. **Defaults:** min order 0.1 kWh, 30 orders/min/address, 50 open orders/maker, quote margin 60 s, listener depth 2 (Anvil) / 6 (Sepolia).
8. **Publish-only Redis events** for orderbook/trades/user channels (no WebSocket work).
9. **EIP-2098 compact signatures are rejected** in v1 (the contract would accept them; supporting them is cheap later).
10. **Matching/relaying/listening run embedded in the API process** behind leader locks (with `python -m app.trading.run` for a standalone process), mirroring Phase 2/3.

## Owner changes (from "go")

All ten decisions approved, with these additions (they override the text above where they differ):

- **Soft vs hard cancel.** `DELETE /orders/{hash}` on an order with an in-flight reservation (`reserved_wh > 0`) never touches the reserved part. It cancels only the unreserved remainder (`amount − filled − reserved`), sets `cancel_pending = true` and returns the resulting state (`status`, `cancel_pending`, `cancel_type`, `reserved_wh`, `cancellable_wh`, `cancelled_wh`). When the settlement(s) resolve (confirmed → the order is filled for the reserved part, then finalized as cancelled; failed → released Wh are cancelled too), the order is finalized as `cancelled` and `cancel_pending` clears. An order with no unreserved remainder and nothing to cancel returns 409 `nothing_to_cancel` with the same state body; its on-chain cancel is the guarantee.
- **`cancel_type`.** Orders carry `cancel_type ∈ {null, "offchain", "onchain"}` (set on every cancellation: `offchain` for the API/server-side cancels incl. sweeper, expiry-dust, unfunded; `onchain` for `OrderCancelled`/`NonceFloorRaised` observed by the listener or a cancel-race revert). The API exposes `cancel_type` and `cancel_reason` on every order so Phase 6 can label "Cancelled" vs "Cancelled on-chain" and offer "Cancel on-chain (costs gas)". `order-config` returns the exchange address and ABI fragments for that action.
- **ADR-004** records that off-chain cancel is binding only while settlement is relayer-gated; permissionless settlement requires on-chain cancels or signed cancel attestations.
- **Release backoff.** When the relayer releases a pair because no feasible price remains, the pair (`buy_hash`, `sell_hash`) goes into `pair_cooldowns` with exponential backoff (`PAIR_COOLDOWN_SECONDS` base 30 s ×2 per consecutive release, capped at `PAIR_COOLDOWN_MAX_SECONDS` 600 s; resets after a confirmed trade). The matcher skips cooled-down pairs. Each release is logged and counted (`wattswap_settlement_releases_total{reason}`), and an integration test shows a price move that makes a pair un-settleable does not cause a re-match loop.
- **Dust reason.** `cancel_reason = "dust_remainder"` is kept as a stable code so Phase 6 can display it as "Filled — remainder below minimum closed".
