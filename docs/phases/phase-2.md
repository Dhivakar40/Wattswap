# Phase 2 — Backend core + meter pipeline

Branch: `phase-2-backend-core` · Target tag: `v0.2.0` (after approval)

Status: **IMPLEMENTED — awaiting PR approval.** Owner decisions on the plan are recorded under *Decisions* (overrides marked).

## Outcome

See *What was built*, *How to verify* and *Known limitations* at the end of this file.

## Starting state

- Phases 0–1 merged. Contracts deployed by `make deploy-local`; ABIs/addresses in `packages/shared` (anvil addresses are the deterministic ones from account #0).
- `services/api` has only `main.py` (`/health`), `core/{config,logging}.py`, empty package dirs, an empty Alembic env. `services/meter-sim` is an empty package (no deps).
- Mint path on-chain (Phase 1): `EnergyToken.mint(Reading, deviceSig)` needs `MINTER_ROLE` caller + EIP-712 `Reading{meterId, readingId, amountWh, intervalStart, intervalSeconds}` signed by the meter's `deviceSigner` registered in `MeterRegistry`. Registration needs `REGISTRAR_ROLE`. `readingId` replay-protected on-chain.
- Local tooling: Docker, forge/anvil 1.8.5, uv 0.13 present. Host Python is 3.10; uv provisions 3.12 for the projects.

## Scope

In: DB schema + migrations (all 8 tables), SIWE auth → JWT cookie, meter registry REST + on-chain registration, meter-simulator service, reading ingest → verify → mint pipeline with idempotency, §5.6 backend basics (error envelope, request IDs, JSON logs, `/ready`, `/metrics`, Redis rate limiting, CORS), an Anvil end-to-end integration test, CI job for it, docs.
Out: pricing engine, order book/matcher/relayer/chain listener (Phase 3–4, though their *tables* ship now), WebSockets (Phase 5), frontend. `orders/trades/price_ticks/settlements` get models + migration only — no endpoints or logic.

## Architecture of the mint pipeline

```
meter-sim --POST /api/v1/readings (EIP-712 signed by device key)--> API ingest
   1. schema validation, rate limit (per meter)
   2. meter exists + active in DB; recover signer from sig == meter.device_signer
   3. off-chain plausibility (same formula as MeterRegistry.maxEnergyWh) and interval sanity
   4. INSERT readings (UNIQUE reading_id)  -> duplicate => 200 with the existing record (idempotent)
   5. INSERT mints(status=pending) in the same DB transaction
mint worker (asyncio task, Redis lock, single writer):
   pending -> submitted (tx hash persisted BEFORE broadcast confirmation) -> confirmed | failed
   - pre-check readingMinted(readingId) on-chain; if already minted, adopt it (confirmed, no tx)
   - web3.py v7 AsyncWeb3, ORACLE key, nonce from chain 'pending', estimate gas, retry w/ bumped fee, N confirmations
   - revert reasons decoded from the ABI custom errors and stored (`failure_reason`)
   - on restart: re-drive `submitted` rows by tx hash, `pending` rows by readingId pre-check
```

Idempotency layers: (1) DB `UNIQUE(reading_id)` + `UNIQUE(meter_id, interval_start)`, (2) `mints.reading_id` unique, one mint row per reading, (3) on-chain `readingMinted`. Re-POSTing the same signed reading never produces a second mint; re-driving after a crash never double-mints.

## Data model (SQLAlchemy 2.0 async, Alembic)

Conventions: UUIDv4 PKs (internal), `created_at/updated_at` timestamptz; addresses stored lowercase `varchar(42)` with a checksum-normalizing type; on-chain integers as `Numeric(78,0)`; Wh as `BigInteger`; status columns are Postgres enums.

| Table | Key columns |
|---|---|
| `users` | `address` (unique), `role` (user/admin), `last_login_at` |
| `meters` | `meter_id` (bytes32, unique), `owner_id` → users, `device_signer`, `label`, `rated_capacity_w`, `lat_e6`, `lon_e6`, `active`, `is_simulated` (always true in v1), `register_tx_hash`, `register_status` (pending/confirmed/failed) |
| `readings` | `reading_id` (bytes32, unique), `meter_id` → meters, `amount_wh`, `interval_start`, `interval_seconds`, `signature`, `received_at`; `UNIQUE(meter_id, interval_start)` |
| `mints` | `reading_id` → readings (unique), `status` (pending/submitted/confirmed/failed), `tx_hash`, `block_number`, `attempts`, `failure_reason` |
| `orders` | EIP-712 order fields + `signature`, `order_hash` (unique), `status`, `filled_wh`, `cancel_reason` — **schema only** |
| `trades` | `buy_order_id`, `sell_order_id`, `fill_wh`, `exec_price`, `fee`, `tx_hash`, `log_index` (`UNIQUE(tx_hash, log_index)`) — **schema only** |
| `price_ticks` | `ts`, `market_id`, `raw_price`, `price`, `supply_wh`, `demand_wh`, `signature`, `stale` — **schema only** |
| `settlements` | `trade_id`, `status` (matched/submitted/confirmed/failed), `tx_hash`, `attempts`, `failure_reason` — **schema only** |

Also: `auth_nonces` live in Redis, not Postgres. Migration test: `alembic upgrade head` → `downgrade base` → `upgrade head`, plus a check that models and migrations have no drift (`alembic check`).

## Auth: SIWE → JWT cookie

- `GET /api/v1/auth/nonce` → random nonce stored in Redis (TTL 5 min, single use).
- `POST /api/v1/auth/login {message, signature}`: parse EIP-4361, validate domain (config `SIWE_DOMAIN`), chain id, nonce (consumed atomically), expiry/not-before; verify via EOA recovery **and EIP-1271** for contract wallets. Upsert user. Set `wattswap_session` cookie: JWT HS256 (`JWT_SECRET` env, required outside dev), 30-min TTL, `HttpOnly`, `SameSite=Lax`, `Secure` outside dev, `jti` + `sub=address`.
- `POST /api/v1/auth/logout` clears cookie; `GET /api/v1/auth/me`. Admin role comes from `ADMIN_ADDRESSES` env allow-list (for the Phase 6 admin panel).
- CSRF: cookie is `SameSite=Lax` and state-changing endpoints require JSON content type; documented in the threat model. Browser origin is CORS-locked.

## API surface (`/api/v1`)

| Endpoint | Auth | Notes |
|---|---|---|
| `GET /auth/nonce`, `POST /auth/login`, `POST /auth/logout`, `GET /auth/me` | – / cookie | above |
| `POST /meters` | user | body: `label`, `device_signer` (address), `rated_capacity_w`, `lat`, `lon` (decimal degrees, converted to ×1e6 ints). Server derives `meter_id = keccak256(owner ‖ device_signer ‖ salt)`, calls `MeterRegistry.registerMeter` with the REGISTRAR key, returns meter with `register_status`. |
| `GET /meters`, `GET /meters/{id}` | user (own only; admin all) | includes totals minted |
| `PATCH /meters/{id}` | owner | `label`, `active` (mirrors `setActive` on-chain) |
| `POST /readings` | device signature | the ingest endpoint. Not cookie-authenticated: authenticity is the EIP-712 signature. |
| `GET /meters/{id}/readings`, `GET /mints` | owner | paginated (cursor), with mint status + tx hash |
| `GET /health`, `GET /ready` (db, redis, rpc, chain id, role checks), `GET /metrics` | – | Prometheus |

Cross-cutting: consistent error envelope `{error:{code,message,details,request_id}}`, `X-Request-ID` propagation, structlog-style JSON logs with request id, Redis sliding-window rate limits (per IP, per user, per meter on ingest), CORS from config, OpenAPI at `/docs`. Pydantic v2 everywhere; bytes32/address validated types.

## Meter simulator (`services/meter-sim`) — labeled, isolated

- Every reading carries `"simulated": true` in the HTTP body (not part of the signature) and the service logs/README/UI field `is_simulated` make this explicit. It talks to the API **only** over the public ingest endpoint, so replacing it with hardware means replacing this one process.
- Interface `MeterDevice` (Protocol): `read_interval(start, seconds) -> Reading`. `SimulatedMeter` is the sole implementation.
- Generation model (pure, unit-tested): solar position from lat/lon/time (NOAA-style declination + hour angle → elevation) → clear-sky irradiance (simple Haurwitz model) → scaled by Open-Meteo `shortwave_radiation` for that hour when available (httpx, timeout, retry/backoff, fall back to clear-sky × a configured cloud factor with the fallback logged) → `capacity_w × irradiance/1000 × performance_ratio(0.8) × temp_derate` → multiplicative noise (seeded RNG, configurable σ, e.g. 3%) → integer Wh, clamped to ≥0 and to the registry plausibility ceiling.
- Deterministic mode (`--seed`, `--no-weather`) for tests and CI; a `--speedup` flag emits backdated whole intervals quickly for demos (intervals are always in the past, as the contract requires `intervalStart + intervalSeconds ≤ block.timestamp`).
- EIP-712 signing with `eth_account` using the shipped domain (`name "WattSwap Energy"`, `version "1"`, chain id, EnergyToken address). Verified against `packages/shared/vectors/eip712.json` in a test, so Python and Solidity can't drift.
- Config: a `meters.json` (or env) listing meters with their own device private keys (dev-only, generated by `meter-sim keygen`, which prints the address to register). Keys live in gitignored `.meter-keys/` or env, never committed. `readingId = keccak256(meterId ‖ intervalStart ‖ intervalSeconds)` so the same interval always yields the same id (retries are naturally idempotent).
- Runs as a docker-compose service `meter-sim` (profile `sim`, off by default so `make up` stays quiet) plus `make sim`.

## Files (high level)

```
services/api/app/
  core/{config,logging,errors,middleware,ratelimit,db,redis,metrics}.py
  models/*.py           schemas/*.py     routers/{auth,meters,readings,health}.py
  auth/{siwe,jwt,deps}.py
  services/{meters,readings,mints}.py
  chain/{client,contracts,eip712,tx}.py     # AsyncWeb3 wrapper, ABI loading from packages/shared, nonce/gas/retry
  workers/mint_worker.py
services/api/alembic/versions/0001_initial.py
services/api/tests/{unit,integration}/...
services/meter-sim/meter_sim/{solar,weather,device,signing,client,cli}.py + tests
docker-compose.yml (+meter-sim), .env.example (+JWT_SECRET, SIWE_DOMAIN, REGISTRAR_PRIVATE_KEY, ADMIN_ADDRESSES, MINT_CONFIRMATIONS, ...)
Makefile (+ migrate, sim, test-integration), .github/workflows/ci.yml (+ api-integration job)
docs/{api.md, architecture.md (update), threat-model.md (update)}, docs/adr/005-mint-pipeline-idempotency.md, 006-auth-siwe-jwt.md
```

New Python deps — api: `PyJWT`, `siwe`, `eth-account` (comes via web3), `structlog` or stdlib JSON formatter (I'll use stdlib, no new dep), `pytest-httpx`/`respx`, `fakeredis` only for unit tests. meter-sim: `httpx`, `eth-account`, `eth-utils`. I will check installed `siwe`/`web3` APIs from source before use.

## Test plan

- **Unit**: SIWE parsing/validation edge cases (bad domain, replayed/expired nonce, wrong chain, wrong signer, 1271 via mock wallet), JWT (expiry, tamper, wrong secret), plausibility math vs. Solidity formula (property test), solar model (night = 0, noon peak, hemisphere/season sanity, capacity ceiling), EIP-712 signing vs. shared vectors, rate limiter, error envelope.
- **API (httpx ASGI + real Postgres/Redis)**: auth flow, meter CRUD ownership isolation (user B can't read A's meter), ingest rejects: unknown meter, inactive meter, bad signature, wrong-key signature, implausible amount, overlapping interval, future interval; duplicate POST is idempotent.
- **Migrations**: up/down/up, no model drift.
- **Integration (the proof)**: marked `integration`; against real Anvil + Postgres + Redis with contracts deployed:
  1. SIWE login as an Anvil wallet → register a meter (device key from sim keygen) → on-chain `MeterRegistry` shows it.
  2. Run the real `meter-sim` code in-process (seeded, no weather) for N backdated intervals → POST to the real ASGI app.
  3. Mint worker confirms; assert `WATT.balanceOf(owner)` equals Σ Wh from readings, `ReadingMinted` events match, DB `mints` all `confirmed` with tx hashes.
  4. Replay every reading → no new rows, balance unchanged. Crash-recovery: kill the worker between broadcast and confirmation (simulated), restart, no double mint.
  5. Failure paths: deactivated meter → `failed` with decoded `MeterInactive`; rotated device key → old-key readings rejected.
- `make test-integration` boots the stack, deploys, runs it; CI job uses service containers for postgres/redis and `anvil` via foundry-toolchain.
- Gates before push: `ruff`, `mypy --strict`, `pytest` (unit + integration), `forge` untouched, coverage report for api (target ≥85% for new code; the 95% target is contracts-only).

## Risks

| Risk | Mitigation |
|---|---|
| Mint tx broadcast succeeds but process dies before status saved → double-submit | tx hash persisted first; on-chain `readingMinted` pre-check; contract rejects duplicate regardless |
| Nonce races between workers | single writer via Redis lock; nonce from `pending` count with local counter; one oracle signer |
| `siwe` lib incompatibility with pydantic/web3 versions | check installed source; fall back to a small in-repo EIP-4361 parser on `eth_account` if needed (recorded as ADR) |
| Clock skew between sim, API and Anvil (`ReadingNotComplete`) | intervals end ≥ 1 interval before now; API rejects future intervals; integration test uses Anvil `evm_setNextBlockTimestamp`/`evm_mine` where needed |
| Open-Meteo unavailable/rate-limited | sim falls back to labeled clear-sky model; tests never hit the network |
| Windows/Git Bash friction (Makefile, docker) | all commands run via `make` in Git Bash as in Phase 1; integration test self-skips with a clear message if stack is down |

## Decisions (owner-confirmed)

1. **Registrar key**: the API holds `REGISTRAR_PRIVATE_KEY` (Anvil #4), granted `REGISTRAR_ROLE` by `deploy-local`. **Added by the owner:** it must be a distinct address from the oracle/relayer/pricer keys; the API refuses to boot if any two role keys share an address.
2. **Device keys**: the device generates its own key, the user supplies only the address; the API never sees device private keys.
3. **OVERRIDE, interval ordering fixed on-chain.** `MeterRegistry.lastIntervalEnd[meter]`; `EnergyToken.mint` calls the new `RECORDER_ROLE`-gated `recordReading(meterId, amountWh, intervalStart, intervalSeconds)`, which requires `intervalStart >= lastIntervalEnd` and a positive-length interval, then advances the cursor. Readings are minted in order; offline devices backfill oldest-first. Unit + fuzz + invariant tests added; ABIs and vectors regenerated; coverage stays at 100% lines. ADR-003 and the threat model updated. These were the first commits of the phase.
4. **OVERRIDE, JWT lifetime 12 h**, `HttpOnly; Secure; SameSite=Lax`, explicit logout that revokes the token through a Redis denylist. `ADMIN_ADDRESSES` allow-list approved.
5. **Interval 900 s default, configurable** (`READING_INTERVAL_SECONDS`), and the **simulator has a DEMO mode** with time acceleration (default one simulated interval per 20 real seconds, logged as `ACCELERATED DEMO MODE`). **Plausibility uses each reading's own interval length** in the API, simulator and contract, never a hardcoded 900.
6. **The end-to-end Anvil test is the CI gate and runs in demo mode.**

## Acceptance criteria

- [x] `alembic upgrade head` creates all 8 tables; downgrade/upgrade round-trips; no drift.
- [x] SIWE login sets an httpOnly JWT cookie; replayed nonce rejected.
- [x] Meter registered via API appears in `MeterRegistry` on Anvil.
- [x] Simulator readings (EIP-712) are verified, stored once, and minted: `WATT` balance == Σ Wh; replays are no-ops.
- [x] Failure paths covered (bad sig, implausible, inactive meter, rotated key, crash recovery).
- [x] `ruff`, `mypy --strict`, unit + integration pytest green locally and in CI; `/docs`, `/ready`, `/metrics` work.
- [x] README, `docs/api.md`, architecture + threat-model updates, ADRs 005/006, phase doc "what was built / how to verify / limitations" completed.

## Commit plan (Conventional Commits, small)

`docs: phase 2 plan` → `feat(api): core infra (config, errors, logging, db, redis, ratelimit)` → `feat(api): models and initial migration` → `feat(api): siwe auth and jwt cookie` → `feat(api): chain client and eip-712 helpers` → `feat(meter-sim): solar model, weather, signing` → `feat(api): meter registry endpoints` → `feat(api): reading ingest and mint worker` → `feat(meter-sim): cli and service` → `test(api): end-to-end anvil integration` → `ci: api integration job` → `docs: api, adrs, threat model, phase 2 outcome`.

## What was built

- **Contracts** (first commits): ordered, non-overlapping intervals per meter; `recordReading`; `RECORDER_ROLE` wiring in `Deploy.s.sol`; reentrancy-friendly `mint` ordering (Slither clean). 112 Foundry tests, 100% line coverage (215/215).
- **API** (`services/api`): settings with role-key separation check; error envelope, request ids, JSON logs, Prometheus metrics, Redis rate limits, CORS allow-list; async SQLAlchemy models for all eight tables and an Alembic migration (up/down/up and drift-free); SIWE login with EIP-1271, 12 h JWT cookie and Redis-denylist logout; meter registry endpoints that register on-chain with the registrar key (plus label, activate/deactivate and device-key rotation); device-signed `POST /readings` ingest; the mint worker with persist-before-broadcast, adoption, fee-bump replacement, decoded reverts and leader lock; `/ready` verifying real on-chain role wiring.
- **Meter simulator** (`services/meter-sim`): labeled SIMULATOR behind a `MeterDevice` protocol; NOAA solar geometry + Haurwitz clear-sky + Open-Meteo irradiance/temperature (validated, retried, with a reported clear-sky fallback) + PV model (PR 0.8, temperature derate) + seeded noise; EIP-712 signing verified against the Foundry-generated shared vector; real-time and accelerated demo runner; `keygen`, dev-only `provision`, and `run` CLI; Docker image and compose profile.
- **Shared**: plain JSON ABIs (`packages/shared/src/abis/json`) for non-TypeScript consumers.
- **Infra/CI**: API image now builds from the repo root; `make migrate | sim | sim-provision | test-integration`; CI api job installs Foundry, requires services (`REQUIRE_SERVICES=1`) and runs the end-to-end gate as its own step.
- **Docs**: `docs/api.md`, ADR-005 (mint pipeline), ADR-006 (auth and key separation), architecture, threat model (B-1..B-14), README.

## Deviations from the plan

- Rotated-key coverage needed an API surface, so `PATCH /meters/{id}` also accepts `device_signer` (owner-initiated rotation, registrar-signed).
- The integration suite has three tests (full pipeline in demo mode, night intervals, and the same pipeline over real HTTP via uvicorn) rather than one.
- `readings.reading_id` is enforced to be the derived id (stricter than the contract), recorded in ADR-005 and `docs/api.md`.
- API unit/API-level test directories were reorganised into `tests/{unit,api,integration}`.

## How to verify

```bash
make lint                      # contracts fmt, ruff, mypy --strict, eslint, tsc, prettier: all clean
make test-contracts-ci         # 112 tests with the heavy fuzz/invariant profile
make coverage-contracts        # 100% lines (gate raised from 95%)
make slither                   # no high/medium
make up && make test-api       # needs postgres + redis (the compose stack); spawns its own Anvil
make test-integration          # only the end-to-end gate; FAILS (does not skip) if a service is down
make deploy-local && make sim  # live demo against the Docker stack; watch WATT arrive for account #5
```

## Known limitations / tech debt

- Role keys are plain environment variables (KMS/HSM in production).
- Only EOA device signatures are verified at ingest; EIP-1271 device wallets work on-chain but would be rejected by the API.
- Mint throughput is one tx per confirmation round per oracle key (fine for 15-minute readings).
- A reading is `failed` only on a decoded contract-level rejection (ADR-005); it then becomes an unfillable gap by design (the contract cannot go backwards), visible with its reason in `GET /meters/{id}/readings`. RPC, timeout, nonce and gas problems retry forever with backoff.
- `AdminDep` and the admin allow-list are wired and the role is exercised, but no admin-only endpoint exists until Phase 6.
- `ORDERS/TRADES/PRICE_TICKS/SETTLEMENTS` are schema only; no endpoints.
- Accelerated demo mode is catch-up pacing against real chain time, not chain time travel, so it only replays the past (it stops accelerating once it reaches the present).
- Open-Meteo is not called in tests (the simulator's fallback is exercised instead); the live call was run manually against the real API.

