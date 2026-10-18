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
