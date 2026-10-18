# Phase 1 — Smart contracts

Branch: `phase-1-contracts` · Target tag: `v0.1.0` (after approval)

Status: **IMPLEMENTED — awaiting PR approval.** Decisions below were confirmed or overridden by the owner; overrides are marked.

## Starting state

- Phase 0 merged. `contracts/` has a Foundry scaffold (solc 0.8.24, cancun, OZ + forge-std in `lib/`), a throwaway `Version.sol` + test (removed in this phase), empty `script/`.
- `packages/shared` exports `CHAIN_IDS`, `ContractName`, and an empty `ADDRESSES` map.
- Tooling: `forge`/`anvil` 1.8.5 are installed in `~/.foundry/bin` (not on the default shell PATH). **Slither is not installed** (Python 3.10 is available; `pip install slither-analyzer` should work, solc is fetched by `solc-select`/forge).

## Scope

In: the five contracts of §5.1, tests (unit / fuzz / invariant), deploy script for Anvil + Sepolia, ABI + address export to `packages/shared`, coverage + Slither runs, `docs/settlement.md`, `docs/threat-model.md`, ADRs, CI Slither + coverage jobs.
Out: any backend/relayer/frontend code, real meters, mainnet deployment. Sepolia *script* ships now; the actual Sepolia deploy is Phase 8.

## Contracts

| Contract | Summary |
|---|---|
| `EnergyToken` | ERC-20 `WATT`, **3 decimals** (1.000 WATT = 1 kWh, 1 unit = 1 Wh). `AccessControl` + `Pausable` + `ERC20Permit`. `mint(Reading reading, bytes deviceSig)` requires `MINTER_ROLE` caller **and** a valid EIP-712 `Reading{meterId, readingId, amountWh, intervalStart, intervalSeconds}` signature from the meter's device signer (EOA or EIP-1271). Asks `MeterRegistry` to validate owner/active/plausibility, mints to the owner, records `readingId` as used (`ReadingAlreadyMinted`). Emits `ReadingMinted`. |
| `MeterRegistry` | `meterId (bytes32) → {owner, deviceSigner, ratedCapacityW, latE6, lonE6, active}`. `REGISTRAR_ROLE` registers / **rotates `deviceSigner`** / updates / deactivates. `validateReading(...)` view (superseded in Phase 2 by the state-changing `recordReading(meterId, amountWh, intervalStart, intervalSeconds)`, see ADR-003 amendment): meter active, `intervalSeconds` within `[min,max]`, and `amountWh ≤ capacityW × intervalSeconds / 3600 × safetyBps / 10000`. Returns owner. Safety factor and interval bounds are admin-settable with caps. |
| `MockUSDC` | 6-decimal ERC-20 `mUSDC`, public `faucet()` rate-limited per address (fixed amount per cooldown). Testnet only; header comment and name say so. |
| `PriceOracle` | `PRICER_ROLE` pushes `(price, timestamp)` (6dp mUSDC per kWh). Monotonic timestamps, no future timestamps, `maxStaleness` admin-settable. Views: `latest()`, `isFresh()`. |
| `WattSwapExchange` | Atomic settlement, see below. |

### WattSwapExchange design

- **Order** (EIP-712): `{maker, marketId, side, amountWh, limitPrice, expiry, nonce, salt}`; `settle` requires `buy.marketId == sell.marketId` (v1 runs one market, the type never needs a breaking change); `marketId` is included in `TradeSettled`; `side` 0 = buy, 1 = sell; `limitPrice` is mUSDC (6dp) per kWh.
- **PriceQuote** (EIP-712): `{price, timestamp, nonce}` signed by an account holding `PRICER_ROLE` on the `PriceOracle`. Freshness uses `PriceOracle.maxStaleness()`, so there is one staleness parameter. Exec-price band `bandBps` is an **admin-settable** parameter on the exchange (`setBand`, emits `BandUpdated(old,new)`, capped).
- `settle(buy, buySig, sell, sellSig, fillAmountWh, execPrice, quote, quoteSig)`, `RELAYER_ROLE` + `nonReentrant` + `whenNotPaused`. Checks, in order: sides correct, makers differ, `fill > 0`, not expired, nonce ≥ maker's `minNonce`, not individually cancelled, signatures valid (`SignatureChecker` ⇒ EOA + EIP-1271), remaining ≥ fill on both, `sell.limit ≤ exec ≤ buy.limit`, quote signer has `PRICER_ROLE`, quote fresh, `|exec − quote.price| ≤ band`. Effects: update both filled amounts. Interactions: `WATT` seller→buyer, `mUSDC` buyer→seller, `mUSDC` buyer→treasury (fee). Any failure reverts the entire tx.
- **Amount math** (to be fully specified in `docs/settlement.md`): `cost = fillWh × execPrice / 1000` (Wh→kWh), rounded down; `fee = cost × feeBps / 10000`, rounded down; seller receives `cost − fee`. `feeBps ≤ MAX_FEE_BPS (500)`. `cost == 0` reverts (`DustFill`), so a fill can never move energy for free.
- `cancel(order)` and `cancelUpTo(nonce)` (maker-only, `nonce` must be monotonic). Admin: `setFee`, `setTreasury`, `setBand`, `pause/unpause`.
- Custom errors everywhere; events: `TradeSettled`, `OrderCancelled`, `NonceFloorRaised`, `FeeUpdated`, `TreasuryUpdated`, `BandUpdated`.

## Decisions (owner-confirmed)

1. **Fill accounting:** `filled[orderHash]`, `cancelled[orderHash]`, per-maker `minNonce`. ADR-002.
2. **Quote signer** checked against `PRICER_ROLE`; staleness from oracle `maxStaleness`; band admin-settable with event.
3. **OVERRIDE — meter readings verified on-chain.** Registry stores a per-meter `deviceSigner` (set at registration, rotatable by `REGISTRAR_ROLE`). `EnergyToken.mint` needs `MINTER_ROLE` + valid EIP-712 `Reading` signature from that key + owner/active/plausibility/unique `readingId`. Tests: wrong signer, rotated key, replayed readingId. ADR-003.
4. **OVERRIDE — `bytes32 marketId` in `Order`** now; `settle` requires equal market ids; included in `TradeSettled`.
5. **DustFill** revert when `cost == 0`.
6. **Slither** via `uv tool install slither-analyzer`.
7. ADR-004: path to permissionless settlement.
8. **Shared EIP-712 test vectors** (`packages/shared/vectors/eip712.json`: domain, types, message, digest, signer, signature) generated by Foundry from the real contracts, so Phase 4 Python code verifies against identical vectors.
9. Housekeeping: `~/.foundry/bin` added to `~/.bashrc`; first commit is this plan.

## Files

```
contracts/src/{EnergyToken,MockUSDC,MeterRegistry,PriceOracle,WattSwapExchange}.sol
contracts/src/interfaces/{IMeterRegistry,IPriceOracle}.sol
contracts/src/libraries/OrderLib.sol            # typehashes + hashing
contracts/test/unit/*.t.sol                     # one file per contract, every revert path
contracts/test/fuzz/ExchangeFuzz.t.sol
contracts/test/invariant/{ExchangeInvariant.t.sol, handlers/ExchangeHandler.sol}
contracts/test/utils/{Base.t.sol, SigUtils.sol, MockSmartWallet.sol}   # 1271 wallet
contracts/script/{Deploy.s.sol, HelperConfig.s.sol}
contracts/slither.config.json
packages/shared/scripts/export-abis.mjs         # reads out/ + broadcast/ -> src/
packages/shared/src/{abis/*.ts, addresses.json, index.ts}
docs/{settlement.md, threat-model.md, adr/002-*.md, adr/003-*.md, adr/004-*.md}
packages/shared/vectors/eip712.json
docs/phases/phase-1.md (updated at end) · Makefile, ci.yml, README (contracts section)
```
`Version.sol` / `Version.t.sol` are deleted.

## Tests

- **Unit:** every custom error has at least one test that triggers it; happy paths; events; role gating; 1271 wallet path; pause behaviour; expiry/stale-quote boundaries (exactly at limit passes, one past fails).
- **Fuzz:** amounts / prices / fills / fee bps: balances conserved, `seller_in + fee == cost`, never reverts with arithmetic panic, band edges.
- **Invariants** (handler with bounded actors doing settle / partial fills / cancel / cancelUpTo / mint): (a) WATT total supply only changes via mint, and sum of balances == supply; settlements don't change supply; (b) `filled[order] ≤ order.amountWh` for every order seen; (c) mUSDC: buyer debits == seller credits + treasury credits per trade, fee ≤ cost, no underflow; (d) exchange never holds token balances.
- Invariant runs raised in a `ci` profile (e.g. 256 runs × depth 64).

## Deploy + export

- `Deploy.s.sol` reads `PRIVATE_KEY`/role addresses from env (never committed), deploys in dependency order, wires roles (exchange gets no mint rights; relayer gets `RELAYER_ROLE`; pricer gets `PRICER_ROLE`; oracle/minter gets `MINTER_ROLE`), and writes the broadcast file. Network selected by `--rpc-url` (`anvil`, `sepolia` entries in `foundry.toml`, URL from env).
- `export-abis.mjs` produces typed `as const` ABIs (viem-compatible) and updates `addresses.json` per chain id from `broadcast/Deploy.s.sol/<chainId>/run-latest.json`. `ADDRESSES` in `index.ts` loads from it. Root targets: `make deploy-local`, `make export-abis`.

## Quality gates

- `forge fmt --check`, `forge build` (no warnings), `forge test` (default + `ci` profile), `forge coverage` ≥ 95% line on `src/`.
- Slither: zero high/medium; every other triaged finding either fixed or justified in `docs/threat-model.md`.
- `pnpm -r typecheck lint test` for the shared package; local Anvil deploy + export run end to end.
- CI: add Slither and coverage-threshold steps to the `contracts` job.

## Risks

- Windows + Slither/solc-select friction → fallback Docker image.
- Coverage on `forge coverage` can undercount through-IR; if so I'll use `--ir-minimum` and note it.
- Rounding: floor on cost and fee means tiny fills favour the buyer by < 1 micro-mUSDC; `DustFill` guard and tests pin this behaviour.
- EIP-1271 wallets can change validity between signing and settle; documented as accepted risk in the threat model.
- Relayer is trusted for ordering/censorship in v1; bounded by band, limits and quote freshness (documented, ADR-003).

## Acceptance criteria

1. `forge test` passes including fuzz and invariants; coverage ≥ 95% lines.
2. Slither: no high/medium unresolved; all justified in `docs/threat-model.md`.
3. `make deploy-local` against a running Anvil succeeds and `packages/shared` contains ABIs and anvil addresses; `pnpm --filter @wattswap/shared test` passes.
4. `docs/settlement.md` explains the EIP-712 formats and atomicity; ADR-002/003/004 written.
5. PR opened from `phase-1-contracts`, §9 report delivered.

## Commit plan (Conventional Commits)

1. `docs: add phase 1 plan`
2. `feat(contracts): add MeterRegistry and EnergyToken` (+ tests)
3. `feat(contracts): add MockUSDC and PriceOracle` (+ tests)
4. `feat(contracts): add WattSwapExchange with atomic settle` (+ unit tests)
5. `test(contracts): add fuzz and invariant suites`
6. `feat(contracts): add deploy scripts for anvil and sepolia`
7. `feat(shared): export abis and addresses`
8. `ci: add slither and coverage gates`
9. `docs: settlement, threat model, ADRs, phase 1 outcome`

---

## Outcome (what was built)

Deviations from the plan above, all small:

- `PriceQuote` is `{marketId, price, timestamp}` (the planned `nonce` was dropped: quotes are valid while fresh, so a
  nonce would be an unused field). `marketId` was added to the quote so a quote cannot be used for another market.
- `EnergyToken.mint(Reading, deviceSig)` replaces `mintReading(...)` (ADR-003). The signed `Reading` is
  `{meterId, readingId, amountWh, intervalStart, intervalSeconds}`; the interval must be complete.
- Added `setMarket` (enabled markets) so arbitrary `marketId`s cannot settle. ADR numbers: 002 fill accounting,
  003 on-chain meter verification, 004 permissionless settlement path.
- `settle` takes the three signatures as `bytes memory` (not `calldata`): the legacy codegen hits "stack too deep"
  with 8 parameters otherwise (viaIR is avoided so `forge coverage` stays accurate). Costs a little gas.
- Deploy script `Deploy.s.sol` refuses chains other than 31337 and 11155111.

## How to verify

```
cd contracts
forge fmt --check && forge build
forge test                                  # 90 tests (unit, fuzz, invariant, vectors, deploy)
FOUNDRY_PROFILE=ci forge test               # 2048 fuzz runs, 256 invariant runs x depth 64
bash ../scripts/check-coverage.sh           # >= 95% line coverage gate
slither . --config-file slither.config.json --fail-medium
anvil &                                      # then, with PRIVATE_KEY etc. (see make deploy-local):
make deploy-local                           # needs GNU make; the equivalent commands are in the Makefile
pnpm --filter @wattswap/shared test typecheck lint fmt:check
```

## Known limitations

- Windows dev box lacks GNU `make`; the Makefile targets were not run through `make` locally. The commands they wrap
  (forge, slither, coverage script, deploy script, exporter) were each run directly. CI runs them through `make`.
- Sepolia deployment is scripted but not executed (Phase 8; needs funded key and RPC URL).
- Slither: 6 Low (`timestamp`) and 1 Informational remain by design (threat-model.md).
- No external audit.
