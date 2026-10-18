# Threat model (contracts, Phase 1)

Scope: `MeterRegistry`, `EnergyToken`, `MockUSDC`, `PriceOracle`, `WattSwapExchange`. Backend, WebSocket and frontend
threats are added in the phases that build them. This is a **testnet/demo** system: meters are simulated and mUSDC
is worthless; the model still treats the contracts as if they held real value.

## Assets and actors

| Actor | Trust |
|---|---|
| Maker (buyer/seller wallet) | Untrusted; protected by what they sign. |
| Relayer (`RELAYER_ROLE`) | Semi-trusted: ordering/censorship only (ADR-004). |
| Pricer (`PRICER_ROLE`) | Trusted for reference price; bounded by staleness and the on-chain band. |
| Minter/oracle (`MINTER_ROLE`) | Semi-trusted: cannot mint without a device signature (ADR-003). |
| Device key (per meter) | Trusted for that meter's readings only; rotatable. |
| Registrar (`REGISTRAR_ROLE`) | Trusted to register meters honestly. |
| Admin (`DEFAULT_ADMIN_ROLE`) | Fully trusted (fee/band/market/pause/role grants). Multisig in production. |

## Threats and mitigations

| ID | Threat | Mitigation | Tests |
|---|---|---|---|
| T-1 | Relayer debits a maker without authorization | Every debit is bound to a maker EIP-712 signature (amount, limit, expiry, market); tampering invalidates it | `test_revert_signatureForTamperedOrder`, `testFuzz_signatureBindsAllFields` |
| T-2 | Replay of an order (same chain, other chain, other deployment) | Domain includes chainId + verifyingContract; fills tracked per order hash | unit + fuzz overfill |
| T-3 | Overfill / double fill | `filled[hash]` with `Overfill` check; invariant `filled <= amount` | `invariant_noOverfill`, `testFuzz_partialFills_neverOverfill` |
| T-4 | Relayer manipulates price | Exec price must be inside both limits **and** within `bandBps` of a fresh quote signed by `PRICER_ROLE` | `test_revert_priceOutsideBand`, `testFuzz_band`, `testFuzz_limits` |
| T-5 | Stale or forged quote | Freshness against `PriceOracle.maxStaleness`; signer must hold `PRICER_ROLE`; garbage signature handled via `tryRecover` | quote tests |
| T-6 | Non-atomic trade (energy moves, payment doesn't) | Single tx, no swallowed failures, no custody (docs/settlement.md) | atomicity tests, `invariant_*Conserved` |
| T-7 | Reentrancy via token hooks | `nonReentrant`, CEI ordering; tokens are OZ ERC-20 without hooks | n/a (no hook path) |
| T-8 | Free-energy rounding exploit | `DustFill` reverts when `cost == 0`; floor rounding is bounded < 1 micro-mUSDC | `test_revert_dustFill`, fuzz |
| T-9 | Fee abuse / underflow | `feeBps <= 500`; `fee <= cost` by construction; `cost - fee` cannot underflow | `invariant_feeMath`, fuzz |
| T-10 | Fake energy minting | Device signature + `MINTER_ROLE` + plausibility + unique `readingId` + completed interval (ADR-003) | EnergyToken suite |
| T-11 | Reading replay | Global `readingMinted[readingId]` | `test_mint_revertsReplayedReadingId` |
| T-11b | Overlapping / out-of-order intervals double-count energy (key holder signs several readings for the same time) | Per-meter `lastIntervalEnd`; `intervalStart >= lastIntervalEnd` enforced in `MeterRegistry.recordReading` on every mint; only `EnergyToken` holds `RECORDER_ROLE` (ADR-003 amendment) | `test_interval_*`, `MeterIntervalFuzzTest`, `invariant_mintedNeverExceedsCapacityTimesCoveredTime`, `invariant_intervalsDisjoint` |
| T-12 | Compromised device key | Registrar rotates key / deactivates meter; old signatures stop working immediately | `test_mint_rotatedKey_*` |
| T-13 | Compromised pricer key | Impact bounded by band and staleness; admin revokes role; pause | role tests |
| T-14 | Self-trade wash trades | `SelfTrade` revert (does not stop two wallets owned by one person; off-chain surveillance) | `test_revert_selfTrade` |
| T-15 | Admin abuse | Bounded parameters (fee ≤ 5%, band ≤ 20%); every change emits an event; pause only stops flow, never seizes funds | admin tests |
| T-16 | Faucet drain | Per-address cooldown; token is worthless and testnet-only | `MockUSDCTest` |
| T-17 | EIP-1271 wallet changes its mind after signing | Accepted: validity is checked at settle time; cancel/expiry bound the window | 1271 tests |
| T-18 | Maker moves funds away after signing | Settlement simply reverts (atomic); no loss, relayer pays gas. Backend pre-checks balance/allowance | atomicity tests |

Production note: relayer, minter and pricer keys come from env in dev. Production would use a KMS/HSM and a
multisig admin; the pricer must be an EOA key (quote signatures are recovered with ECDSA, not EIP-1271).

## Slither triage

Command: `slither . --config-file slither.config.json --fail-medium` (CI-enforced; `lib/`, `test/`, `script/`
excluded). Result: **no High or Medium findings remain.** Findings that were raised and how they were resolved:

| Detector | Impact | Resolution |
|---|---|---|
| `arbitrary-send-erc20` (WattSwapExchange `_execute`, 3 sites) | High | **Justified, annotated inline.** `from` is the maker whose valid EIP-712/EIP-1271 signature was verified earlier in the same call (T-1). This is the intended signed-order authorization model, as in any off-chain order book; there is no way to implement it without `transferFrom(maker, …)`. |
| `divide-before-multiply` (`fee = cost * feeBps / BPS` after `cost = fill * price / 1000`) | Medium | **Justified, annotated inline.** Deliberate: cost is floored to a whole micro-mUSDC (the real payment amount), then the fee is a floored percentage of that payment. Doing the multiply first would charge a fee on a fractional amount that is never paid. Verified by fuzz: `seller + fee == cost`, `fee <= cost`. |
| `unused-return` (`ECDSA.tryRecover` third value) | Medium | **Justified, annotated inline.** The third return value is the invalid-signature detail (a bytes32) used only for custom error reporting in OZ's `recover`; we only need the recovered address and the error code and revert with `InvalidQuoteSigner` in both failure cases. |
| `timestamp` (EnergyToken, PriceOracle ×2, MockUSDC, WattSwapExchange ×2) | Low | **Accepted.** Comparisons implement expiry, staleness windows (minutes), interval completeness and a one-day faucet cooldown. Validator timestamp drift (seconds) is far below every window and cannot be profitably exploited; boundaries are tested exactly. |
| `naming-convention` (`IPriceOracle.PRICER_ROLE`) | Info | **Accepted.** Mirrors the OpenZeppelin `*_ROLE` constant getter convention. |

Not covered by Slither and therefore reviewed manually: EIP-712 type strings (cross-checked against viem in
`packages/shared/src/vectors.test.ts`), and role wiring in the deploy script (`Deploy.t.sol`).

## Backend and meter pipeline (Phase 2)

| # | Threat | Mitigation | Evidence |
|---|---|---|---|
| B-1 | Login replay / forged SIWE message | Single-use Redis nonce consumed before verification; domain, chain id, `issued_at` window and `expiration_time` enforced | `test_auth.py` (nonce, domain, chain, expiry, stale/future issued-at) |
| B-2 | Stolen session cookie | `HttpOnly; Secure; SameSite=Lax`; 12 h lifetime; logout revokes the `jti` via a Redis denylist | `test_logout_revokes_the_token_server_side` |
| B-3 | JWT forgery (`alg=none`, algorithm swap, wrong secret, tampering) | Algorithm pinned to HS256; `iss/sub/jti/iat/exp` required | `test_tampered_expired_and_wrong_secret_tokens_rejected`, `test_token_rejections` |
| B-4 | Weak JWT secret in production | Process refuses to start without a random secret of 32+ characters | `test_production_requires_a_strong_jwt_secret` |
| B-5 | One compromised key does everything | Distinct oracle/registrar/relayer/pricer keys; boot fails if any two share an address; `/ready` checks real on-chain roles | `test_any_two_role_keys_sharing_an_address_refuses_to_boot` |
| B-6 | Forged readings | EIP-712 device signature checked at ingest (and again by the contract); `reading_id` must be the derived id | `test_signature_failures`, `test_input_validation` |
| B-7 | Reading replay / duplicate mints | `FOR UPDATE` per-meter lock, DB unique constraints, `readingMinted` pre-check, contract replay guard | `test_concurrent_duplicates_*`, integration replay step |
| B-8 | Over-reporting energy | Plausibility ceiling from the reading's *own* interval length, mirrored from the registry; contract re-checks; `lastIntervalEnd` blocks overlap | `test_plausibility_uses_the_readings_own_interval_length`, ADR-003 |
| B-9 | Reading someone else's meters / history | Ownership enforced on every route; foreign ids return 404 | `test_owners_are_isolated_and_admin_sees_all` |
| B-10 | Crash or restart causes double mint or lost mint | Persist-before-broadcast, re-broadcast of the same signed tx, adoption of on-chain mints, fee-bump replacement (ADR-005) | `test_mint_worker.py` |
| B-11 | Two API instances race on the oracle key | Redis leader lock for the worker plus a per-key nonce lock | `test_two_workers_never_double_mint` |
| B-12 | Untrusted weather data poisons the simulator | Open-Meteo responses validated (shape, finite, range); failures fall back to clear-sky and are reported | `test_parse_accepts_valid_and_rejects_garbage` |
| B-13 | Abuse / flooding | Redis rate limits per IP, user, auth endpoint and meter | `test_auth_endpoints_are_rate_limited`, `test_per_meter_rate_limit` |
| B-14 | Information leakage in errors | One error envelope; unhandled errors return a generic 500 and are logged with the request id | `test_error_envelope_*` |

Residual risks for this phase: the dev role keys are plain environment variables (production would use a KMS/HSM);
EIP-1271 device wallets are verified on-chain but not at ingest; the simulator signs whatever its model says, which
is by design for a labeled demo.

## Pricing engine (Phase 3)

| # | Threat | Mitigation | Evidence |
|---|---|---|---|
| P-1 | Malformed, hostile or absurd weather data | Strict validation (shape, lengths, finiteness, ranges); an unusable response is a failure; invalid hours skipped | `test_weather.py` (parse and malformed cases) |
| P-2 | Weather outage or rate limiting silently freezes or distorts prices | Last-good fallback with its real age; stale > 30 min freezes the applied price; `Retry-After` honoured; exponential backoff with jitter; request volume independent of meter count | respx failure-mode tests, `test_stale_weather_trips_the_breaker_*` |
| P-3 | A price shock or manipulation moves the market unnoticed | Raw-move anomaly: widened band hint, stored event, metric; the applied price is rate-limited (0.25 %/tick) and the limiter can never reach the breaker threshold | `test_limiter_alone_can_never_reach_*`, day-trace tests |
| P-4 | Quote from the future or too old is rejected on-chain, halting settlement | Quote timestamps come from the chain-anchored clock, never the (possibly simulated) market clock; refreshed every tick | `test_a_quote_stamped_with_the_simulated_clock_would_be_stale_on_chain`, exchange rejection tests |
| P-5 | Float rounding in prices that are signed or sent on-chain | Decimal arithmetic; integer output; `PriceQuote` rejects non-int; parameter API rejects JSON floats | `test_a_quote_price_must_be_a_positive_plain_int`, `test_prices_and_money_are_integers_*` |
| P-6 | Compromised pricer key | Distinct key (boot refuses shared role keys); impact bounded by the on-chain band, `maxStaleness` and role revocation; production would use a KMS/HSM | ADR-006, exchange quote tests |
| P-7 | Admin sets unsafe parameters | Bounds on every parameter; cross-parameter rules (floor < base < ceiling, limiter vs breaker); role-gated; append-only audit log; atomic validation | `test_invalid_updates_are_rejected_atomically`, audit test |
| P-8 | Two engines quote or push at once | Redis leader lock; per-key nonce lock for the pricer; unique (market, ts) ticks; restart restores state from persisted ticks | `test_only_one_instance_prices_at_a_time`, restart test |
| P-9 | Oracle push failure stalls pricing or spams the chain | Push is a separate loop with backoff (never blocks ticks); threshold + heartbeat + minimum interval | oracle pusher tests |
| P-10 | Start-up race creates duplicate markets | Idempotent `INSERT .. ON CONFLICT DO NOTHING` | `test_concurrent_start_up_creates_the_default_market_exactly_once` |
| P-11 | Wash trading or spoofed bids move the price | Out of scope until the order book exists (Phase 4); bids and fills enter demand with bounded weights and are clamped by the ratio cap | model tests |

## Trading: order book, matcher, relayer, listener (Phase 4)

| # | Threat | Mitigation | Evidence |
|---|---|---|---|
| T4-1 | An order accepted off-chain reverts on-chain (malleable/compact signature) and burns relayer gas | The API enforces OpenZeppelin's exact signature rules (65 bytes, v 27/28, low s); contract wallets via ERC-1271 | `test_order_signatures.py`, `test_signature_and_format_rejections` |
| T4-2 | Python and Solidity disagree on the order hash (silent invalid orders) | Hand-rolled and `eth_account` digests, Foundry-generated vectors incl. edge values, CI-gated byte-identical signatures, `hashOrder` equality on Anvil | `test_order_vectors.py`, `test_posting_the_same_signed_order_twice_*` |
| T4-3 | Replay of an order on another chain or deployment | Domain separator binds chain id and exchange address | `test_domain_binds_chain_and_contract` |
| T4-4 | Over-commitment: many orders against one balance/allowance | Total open commitment per token must fit balance AND allowance at acceptance; sweeper re-checks, newest orders cancelled first | `test_a_maker_cannot_commit_more_than_balance_*`, `test_sweeper_cancels_orders_*` |
| T4-5 | Funding check double-counts a fill in the mined-but-unconfirmed window and cancels healthy orders | Pinned-block snapshot using the chain's `filled` | ADR-008, settlement tests |
| T4-6 | Order spam / relayer gas griefing | Per-address placement limit and open-order cap before any chain call; min order/fill/cost; dry-run and pre-flight before every send; pair cooldown | `test_order_placement_is_rate_limited_*`, `test_no_gas_is_spent_on_dust_sized_fills` |
| T4-7 | Two matchers/relayers act at once and double-allocate | Redis leader locks + `FOR UPDATE` + ownership re-check before commit + DB CHECK on reservations | `test_two_matchers_*`, `test_concurrent_cycles_*`, `test_losing_the_lock_*` |
| T4-8 | Maker cancels on-chain while matched/submitted | Pre-flight check; mined revert classified as cancel race: settlement failed, order removed, counterparty freed, never retried | `test_on_chain_cancel_*` |
| T4-9 | Quote ages out between signing and mining | Validity margin when signing, same-nonce replacement with a fresh quote, `StaleQuote` treated as transient | `test_a_quote_without_enough_validity_*`, `test_a_stuck_settle_is_replaced_*` |
| T4-10 | Crossed pair outside the quote band reverts forever (hot loop) | Band-feasible matching; releases back off exponentially per pair | `test_a_price_move_*_rematch_loop` |
| T4-11 | Crash between signing and broadcast loses or duplicates a settlement | Raw tx persisted before broadcast; restart re-sends the same tx; exactly one `TradeSettled` | `test_relayer_crash_after_signing_*` |
| T4-12 | Reorg leaves the DB claiming a fill that no longer exists | Reversible event application, block-hash window, rollback and re-apply; reconcile repairs drift | `test_listener_rolls_back_a_reorged_settlement_*`, `test_reconciliation_*` |
| T4-13 | Replayed/duplicated logs double-apply fills | UNIQUE `(tx_hash, log_index)`, apply only on insert | `test_listener_is_idempotent_*` |
| T4-14 | Off-chain cancel assumed to be binding | API states `binding_onchain: false`; reserved Wh never cancelled off-chain; ADR-004 addendum for permissionless settlement | `test_off_chain_cancel_*` |
| T4-15 | Relayer key compromise | Distinct role key (boot refuses shared keys); impact bounded by signed orders, quote band, staleness and role revocation; production would use a KMS/HSM | ADR-006 |
| T4-16 | Wash trading / spoofed bids move the engine's price | Bids must be funded to stay on the book; demand weights are bounded and the price is rate-limited; self-trades never match. Cross-account wash trading is not prevented (testnet scope) | pricing tests, `test_a_maker_is_never_matched_with_themselves` |

Residual: the relayer still chooses among valid matches and any price inside limits ∩ band (ADR-004). A maker can
fail a settlement by moving funds after the pre-flight; it costs the relayer one failed transaction and gets that
order cancelled as unfunded.

## Known limitations / out of scope for Phase 1

- No third-party audit. Do not deploy with real value.
- Relayer can choose among valid matches and any price inside the band (ADR-004).
- Readings must be minted in chronological order per meter. A device that was offline backfills oldest-first;
  an interval skipped is a permanent gap. Within the plausibility ceiling, a device key can still *claim* any
  amount up to capacity x time x safety factor for time not yet covered; that residual is inherent to a
  signed-meter model.
- Meter data authenticity ends at the device key; a physically tampered or simulated device signs whatever it is
  told to. For the demo this is by design (labeled simulator).
- Fee-on-transfer / rebasing tokens are unsupported (only the two deployed tokens are used).
- `cancel` of an order whose maker is a contract wallet must be called by that wallet.
