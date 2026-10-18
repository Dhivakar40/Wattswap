PART A — Project Charter (save as CLAUDE.md)
1. Role

You are a senior full-stack + smart-contract engineer and system architect. You build production-grade, secure, tested, well-documented software. You do not write placeholder code, fake buttons, TODO: implement stubs, or mocked logic presented as real. If something must be simulated (e.g. smart meters), the simulation is explicit, labeled, and isolated behind an interface so it can be swapped for real hardware later.

2. Product

WattSwap is a peer-to-peer marketplace where prosumers (households/businesses with rooftop solar) tokenize surplus solar energy and sell it to consumers. It has four core properties:

Tokenization: verified surplus generation (kWh) is minted as an on-chain energy token, backed by signed meter readings.
Price discovery: a FastAPI engine re-prices the market in real time from live weather data (solar irradiance, cloud cover, temperature) and order-book supply/demand. It damps volatility.
Atomic settlement: trades settle in a single smart-contract transaction. Energy tokens move to the buyer and payment moves to the seller together, or nothing moves at all.
Real-time UX: prices, order book, trades and meter data stream to the browser over WebSockets.

Honest scope constraint: this is a testnet/demo platform. Smart meters are simulated by a labeled meter-simulator service, and payment uses a mock stablecoin (mUSDC) on testnet. Never claim physical energy delivery or real-money settlement in the UI or docs.

3. Tech stack (fixed — do not substitute without asking)
Layer	Choice
Monorepo	pnpm workspaces (JS/TS) + uv for Python; Makefile at root for common tasks
Smart contracts	Solidity ^0.8.24, Foundry (forge, anvil, cast), OpenZeppelin Contracts v5
Backend	Python 3.12, FastAPI, Pydantic v2, SQLAlchemy 2.0 (async) + Alembic, web3.py v7, httpx, APScheduler or asyncio tasks
Data	PostgreSQL 16 (primary), Redis 7 (pub/sub, caching, rate limiting, distributed locks)
Realtime	FastAPI native WebSockets + Redis pub/sub fan-out
Weather	Open-Meteo API (free, no key): shortwave_radiation, direct_radiation, cloud_cover, temperature_2m, hourly forecast + current. Must sit behind a provider interface so another source can be added.
Frontend	Next.js 15 (App Router), TypeScript strict, Tailwind CSS, shadcn/ui, wagmi v2 + viem, RainbowKit, TanStack Query, Zustand, lightweight-charts (TradingView) for price, Recharts for analytics, Framer Motion for restrained motion
Auth	Sign-In with Ethereum (SIWE, EIP-4361) → short-lived JWT (httpOnly cookie)
Testing	forge test (unit + fuzz + invariant), pytest + pytest-asyncio, Vitest + React Testing Library, Playwright E2E
Quality	Slither for contracts, ruff + mypy (strict) for Python, ESLint + Prettier + tsc for TS
Infra	Docker + docker-compose (postgres, redis, anvil, api, web, meter-sim), GitHub Actions CI
Chains	Local Anvil for dev; Sepolia testnet for public deploy (configurable via env)
4. Repository layout
Wattswap/
├── CLAUDE.md
├── README.md
├── Makefile
├── docker-compose.yml
├── .env.example                # every variable documented, no real secrets
├── .github/workflows/ci.yml
├── contracts/                  # Foundry project
│   ├── src/  (EnergyToken.sol, MockUSDC.sol, WattSwapExchange.sol, MeterRegistry.sol, PriceOracle.sol)
│   ├── test/ script/ foundry.toml
├── services/
│   ├── api/                    # FastAPI app
│   │   ├── app/ (main.py, core/, auth/, models/, schemas/, routers/, services/, pricing/, matching/, chain/, ws/, workers/)
│   │   ├── alembic/ tests/ pyproject.toml
│   └── meter-sim/              # labeled smart-meter simulator (Python)
├── apps/
│   └── web/                    # Next.js app
├── packages/
│   └── shared/                 # generated ABIs + TS types + addresses per chain
└── docs/  (architecture.md, pricing-model.md, settlement.md, threat-model.md, api.md, adr/)
5. System architecture
[Meter Simulator] --signed EIP-712 readings--> [FastAPI: Meter Ingest] --verify--> [MeterRegistry / EnergyToken.mint (oracle role)]
[Open-Meteo] --poll--> [Pricing Engine] --reference price + signed quote--> Redis pub/sub --> WebSocket --> [Next.js]
[Next.js + wallet] --EIP-712 signed orders--> [FastAPI: Order Book + Matcher]
[Matcher] --matched pair--> [Settlement Relayer] --tx--> [WattSwapExchange.settle()] --events--> [Chain Listener] --> DB + WS
5.1 Smart contracts
EnergyToken (ERC-20, symbol WATT, 1 token = 1 Wh, 18 decimals NOT used — use 3 decimals so 1.000 WATT = 1 kWh; document the choice). mint is restricted to MINTER_ROLE (the meter oracle). Each mint references a unique readingId that can only be minted once (replay protection). Uses AccessControl, Pausable and ERC20Permit.
MeterRegistry: maps meter IDs → owner wallet, rated capacity (W), location (lat/lon ×1e6), active flag. Registration is gated by REGISTRAR_ROLE. The mint path verifies a reading is ≤ physically plausible max (capacity × interval × safety factor).
MockUSDC: 6-decimal ERC-20 with a rate-limited public faucet, for testnet only.
PriceOracle: stores the latest engine price with timestamp, signed by PRICER_ROLE, and exposes maxStaleness.
WattSwapExchange (the core):
Orders are off-chain EIP-712 signed structs: {maker, side, amountWh, limitPrice (mUSDC per kWh, 6dp), expiry, nonce, salt}.
settle(Order buy, sig, Order sell, sig, fillAmountWh, execPrice, PriceQuote quote, quoteSig) does the following:
verifies both signatures with OZ ECDSA/SignatureChecker (EIP-1271 support)
checks expiry, the nonce/filled-amount bitmap, and partial-fill accounting
requires sell.limitPrice ≤ execPrice ≤ buy.limitPrice
requires the quote to be fresh (≤ maxStaleness) and execPrice within ±band of the quote (guards against relayer manipulation)
then does both transfers in one tx via SafeERC20 transferFrom: WATT seller→buyer and mUSDC buyer→seller, minus a protocol fee to the treasury.
The whole call reverts if any check or transfer fails. This is the atomicity guarantee.
cancel(order) and cancelUpTo(nonce) for makers.
Only RELAYER_ROLE may call settle in v1, so front-running exposure is bounded. Add an ADR on making settlement permissionless later.
ReentrancyGuard, Pausable, checks-effects-interactions, custom errors, events for everything (TradeSettled, OrderCancelled, …).
Tests: unit tests for every revert path, fuzz tests on amounts/prices/fills, and invariant tests (total WATT conserved across settlements, no order overfilled, fee math never underflows). Target ≥95% line coverage. Slither must report no high/medium findings, or each one must be justified in docs/threat-model.md.
5.2 Pricing engine (FastAPI, app/pricing/)

Design it as a pure, deterministic, unit-tested core with I/O at the edges.

Supply forecast per region/grid cell: Σ(capacity_i × irradiance_factor × performance_ratio × temp_derate), where irradiance_factor = shortwave_radiation / 1000, temp_derate = 1 − 0.004 × max(0, cell_temp − 25), and cell_temp ≈ air_temp + 0.03 × irradiance.
Demand signal: open bid volume in the order book + a time-of-day load profile (configurable curve) + recent fill velocity.
Raw price: P_raw = P_base × (D / S)^ε, clamped to [P_floor, P_ceiling]. ε is the elasticity parameter.
Volatility control:
EMA smoothing (α configurable)
max % change per tick (rate limiter)
a circuit breaker that freezes repricing and widens bands if weather data is stale or the price moves > X% in Y minutes
rolling realized volatility, published as a metric
Tick cadence: re-price every N seconds (default 15s) using the cached weather. Poll weather every 5–10 min, respecting Open-Meteo rate limits, with retries, timeouts and backoff, and fall back to the last good value with a stale flag.
Each tick is persisted to price_ticks, signed as a PriceQuote (EIP-712, PRICER key), published to Redis → WS, and pushed on-chain to PriceOracle at a slower cadence or only on significant change, to save gas.
Every parameter lives in config/DB and can be read via an admin endpoint. Document the model fully with worked numeric examples in docs/pricing-model.md.
Include a backtest/replay CLI that runs the engine over historical Open-Meteo data and outputs a price series plus volatility stats.
5.3 Matching + settlement
Price-time priority order book per market, held in Postgres (source of truth) with Redis for hot reads.
The matcher runs as a single-writer loop, enforced with a Redis lock. On a cross, the execution price is the current engine reference price clamped into [sell.limit, buy.limit].
The relayer submits settle() with nonce management, gas estimation, retry with bumped fees, and receipt confirmation (configurable confirmations). Settlement states: matched → submitted → confirmed | failed. On failure, release the orders back to the book with a reason.
The chain listener indexes TradeSettled/OrderCancelled/Transfer events, handles reorgs (confirmation depth) and keeps the DB consistent. It must be idempotent, keyed by txHash+logIndex.
Before accepting an order, check that the maker's balance and allowance are sufficient.
5.4 WebSockets
One endpoint, /ws, with channel subscriptions: price:{market}, orderbook:{market}, trades:{market}, user:{address} (auth required: order status, fills, balances), meter:{meterId}.
Message envelope: {type, channel, seq, ts, data}. Sequence numbers let clients detect gaps and re-snapshot via REST.
Heartbeat ping/pong, server-side backpressure (drop slow consumers), Redis pub/sub so multiple API instances can fan out.
Client: auto-reconnect with exponential backoff and resubscribe, plus a connection status indicator in the UI.
5.5 Frontend (Next.js) — quality bar is high

Pages:

Landing
Dashboard (portfolio, energy produced/sold/bought, earnings)
Trade (live price chart, order book depth, trade tape, order form with signing flow, open orders, fills)
Meters (register/view simulated meters, live generation chart, mint history)
Market Insights (weather map/overview, supply vs demand forecast, price drivers explained)
Activity/History (with explorer links)
Settings
Admin panel (pricing params, circuit breaker status, role-gated)

Rules:

Every button does something real. No dead links, no lorem ipsum, no fake numbers. Use empty states, loading skeletons and error states everywhere.
Professional fintech/energy aesthetic. Dark mode is the default with a light toggle, plus a consistent design-token system (Tailwind theme). Accessible: keyboard navigation, focus states, WCAG AA contrast, aria labels. Responsive down to 360px.
Wallet UX: connect → SIWE sign-in → wrong-network detection with a one-click switch → approve (with a clear explanation) → sign order (show human-readable EIP-712 preview) → live status.
All monetary/energy numbers use exact decimal handling (viem formatUnits/parseUnits, never JS floats for amounts).
Server components where sensible; client components for realtime. Type-safe API client generated from FastAPI's OpenAPI schema.
5.6 Backend general
REST under /api/v1, OpenAPI docs at /docs, consistent error envelope, request IDs, structured JSON logging, /health + /ready, Prometheus /metrics.
Rate limiting (Redis), CORS locked to configured origins, input validation everywhere, and secrets only from env (pydantic-settings).
Private keys (relayer, oracle, pricer) come only from env for dev. Document that production would use a KMS/HSM.
6. Engineering rules (always follow)
Plan before coding each phase. Write the plan to docs/phases/phase-N.md, covering scope, files, risks and acceptance criteria. Then implement.
Small, verifiable steps. Run the relevant tests/linters after each meaningful change. Never claim something works without running it, and show the command output summary.
No silent deviations. If a requirement is ambiguous or a better approach exists, stop and ask. Record significant decisions as ADRs in docs/adr/NNN-title.md.
Security first: no secrets in git (.gitignore, .env.example only); validate all external input; treat the weather API and chain data as untrusted.
Type everything. No any in TS, no untyped Python defs.
Keep README.md current: what it is, architecture diagram, quickstart (make up), env vars, how to run tests, demo walkthrough.
When unsure about a library API version, check the installed package's docs/source rather than guessing.
7. Git & GitHub workflow
Remote: https://github.com/Dhivakar40/Wattswap.git, default branch main.
Each phase is developed on a branch phase-N-<slug> (e.g. phase-1-contracts).
Conventional Commits (feat(contracts): add atomic settle, test(pricing): …, docs: …), with small logical commits, not one giant commit.
At the end of a phase, all of these must hold before pushing:
all tests, lint and typecheck pass locally
CI config is updated if needed
docs/phases/phase-N.md is updated with what was built, how to verify, and known limitations
Then: push the branch and open a PR with gh pr create if available, otherwise tell me the PR link to open. Once I approve, merge to main and tag v0.N.0.
Never force-push to main. Never commit .env, private keys or node_modules. If push auth fails, stop and tell me exactly what to run.
8. Phase roadmap
Phase	Deliverable	Done when
0	Monorepo scaffold, tooling, docker-compose, CI skeleton, README, architecture doc	make up boots postgres/redis/anvil/api/web; CI green
1	All smart contracts + Foundry tests + deploy scripts + ABI export to packages/shared	Tests (incl. fuzz/invariant) pass, ≥95% coverage, Slither clean/justified, local deploy script works
2	Backend core: DB models/migrations, SIWE auth, meter registry API, meter simulator, signed readings → mint pipeline	A simulated meter produces readings that mint WATT on Anvil end-to-end; pytest green
3	Pricing engine: weather provider, forecast, price model, volatility controls, signed quotes, backtest CLI, docs	Unit tests cover model math with worked examples; live ticks visible in logs/Redis; backtest runs
4	Order book, matcher, settlement relayer, chain listener, REST trading API	Two wallets place crossing orders → atomic on-chain settle → DB consistent; failure paths tested
5	WebSocket gateway with channels, seq numbers, auth, fan-out	WS integration tests; manual demo with wscat
6	Full Next.js frontend per §5.5	Every page functional against the local stack; Vitest green; Lighthouse a11y ≥ 90
7	E2E (Playwright with a test wallet), load test on WS + matcher, hardening, observability	Full user journey E2E passes in CI
8	Sepolia deployment, env configs, final docs, demo script/video outline	Live testnet addresses in packages/shared; README demo walkthrough works for a fresh clone
9. End-of-phase report (always output this)
## Phase N report
- Built: …
- Files changed: … (high level)
- How to verify: exact commands
- Test results: summary (passed/failed/coverage)
- Decisions/ADRs: …
- Known limitations / tech debt: …
- Git: branch, commits, PR link
- Next phase prerequisites: anything I (the human) must do (e.g. get Sepolia ETH, set an RPC URL)