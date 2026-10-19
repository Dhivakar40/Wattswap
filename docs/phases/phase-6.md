# Phase 6 — Frontend (split into 6a and 6b)

Branches: `phase-6a-foundation` → PR → tag `v0.6.0`; then `phase-6b-pages` → PR → tag `v0.7.0` (tag numbering is the owner's call).

Status: **PLANNED — awaiting "go".** Only 6a is implemented in the first session.

## Starting state

- `apps/web` is the Phase 0 scaffold: Next 15.5, React 19, Tailwind 4, shadcn `Button`, `next-themes`, one server-side API health probe, one Vitest test. No wagmi/viem/RainbowKit/TanStack Query/Zustand yet.
- `packages/shared` exports ABIs (`src/abis`), `addresses.json` (`anvil` populated, `sepolia` empty) and viem as a dependency.
- Auth is an httpOnly `wattswap_session` cookie from `POST /api/v1/auth/login`; `GET /auth/me` returns the user. The web app (`:3000`) and API (`:8000`) are same-site on localhost, so `credentials: "include"` works with the existing CORS allow-list.
- The realtime contract is `docs/api.md` + the reference `services/api/dev/ws_sync.py` (`ChannelSync`: syncing/live, epoch, buffer, gap, duplicate) with its tests in `services/api/tests/realtime/test_sync_protocol.py`. Close code `4401` = session ended; reconnect policy 0.5 s → 30 s with jitter.
- Amount rules (`docs/settlement.md`): WATT = 3 dp (1 Wh = 1 unit), mUSDC = 6 dp, price = mUSDC (6 dp) per kWh, `cost = fillWh * price / 1000`, `fee = floor(cost * feeBps / 10000)`.

## Decisions needed from you (recommendation first)

| # | Question | Recommendation |
|---|---|---|
| D1 | Dashboard needs totals (produced / sold / bought / earnings). The API has only paginated lists (`/trades` ≤ 200 rows, `/mints` cursor). Summing client-side silently under-reports for active users. | Add one small read-only `GET /api/v1/portfolio` (SQL aggregates, per-user, with `as_of`) in 6a, as its own commit and tests. Without it the dashboard would show partial numbers, which breaks "no fake data". |
| D2 | Test the realtime client against "the same scenarios" as Python. | Move the scenarios into one JSON file, `packages/shared/vectors/ws-sync.json`, that **both** pytest and Vitest run. Small edit to the Python test (parametrised reader); the Python reference logic is untouched. Parity is then enforced, not just claimed. |
| D3 | Screenshot script needs a signed-in user with real data. | Run against the local stack (`make up`, `make deploy-local`, `make trade-demo`) with a Playwright-injected EIP-1193 provider backed by an Anvil dev key (test-only code under `apps/web/tests/`, never imported by `src/`). Error/loading states use Playwright route interception. This is Phase 7's wallet harness in embryo; I will keep it reusable. |

If you say "go" without answering, I proceed with the recommendations.

## Scope

### 6a (this session)
Design system and `/dev/ui`; app shell (nav, network badge, connection status, disclosure, toasts); wallet (wagmi/viem/RainbowKit), SIWE, wrong-network switch, session expiry; OpenAPI-generated client with CI drift check; TS realtime client ported from `ws_sync.py`; amounts module; Dashboard; landing page; Playwright screenshot script and visual review; Vitest tests.

### 6b (next session, planned here, not built now)
Trade, Meters, Insights, Activity, Settings, Admin; soft vs on-chain cancel labelling; `dust_remainder` display; failed-reading visibility; live price-driver explanations; approve and EIP-712 order-signing flow with human-readable preview; lightweight-charts and Recharts; Lighthouse a11y ≥ 90 gate; nav links for each page added only when that page exists (so 6a ships no dead links).

## Design tokens (6a)

Defined once as CSS variables in `globals.css`, exposed through Tailwind `@theme`; no raw hex in components (lint rule via a grep check in CI). Dark is default; `:root` is light.

| Group | Tokens |
|---|---|
| Color (OKLCH) | `background`, `surface-1/2/3` (stacked elevation), `border`, `border-strong`, `foreground`, `muted-foreground`, `primary` (electric green-teal, the energy accent), `accent` (amber, sun), `success`, `warning`, `destructive`, `info`, `ring`; data tones `buy` / `sell` (not red/green only, always paired with a label or icon), `chart-1..5` |
| Type | Inter (UI) + JetBrains Mono (numbers, hashes), via `next/font` (self-hosted, no runtime Google request). Scale: `xs 12/16`, `sm 14/20`, `base 16/24`, `lg 18/28`, `xl 20/28`, `2xl 24/32`, `3xl 30/36`, `4xl 40/44`. Tabular figures on all amounts. |
| Spacing | 4 px base: 0, 1, 2, 3, 4, 6, 8, 12, 16, 24 (×4 px); page gutter 16 px at 360, 24 at 768, 32 at 1440; content max-width 1280 |
| Radius | `sm 4`, `md 8`, `lg 12`, `xl 16`, `full` |
| Elevation | `e0` border only, `e1` card, `e2` popover/menu, `e3` dialog; in dark mode elevation is lighter surface + border, shadow secondary |
| Motion | durations `fast 120 ms`, `base 200 ms`, `slow 320 ms`; easings `standard`, `emphasized`; Framer Motion only for enter/exit and number change; every animation respects `prefers-reduced-motion` |
| Contrast | Body and UI text ≥ 4.5:1, large text and focus rings ≥ 3:1, in both themes; verified by axe in the screenshot run and a token-pair unit test that computes the ratios |

## File plan (6a)

```
apps/web/
  next.config.mjs                  pageExtensions gated by WEB_DEV_UI (see /dev/ui)
  src/app/
    layout.tsx  providers.tsx      theme, wagmi, RainbowKit, Query, toasts, session
    page.tsx                       Landing
    dashboard/page.tsx  loading.tsx  error.tsx
    dev/ui/page.dev.tsx            component inventory (only exists in builds with WEB_DEV_UI=1)
  src/components/
    ui/        button, input, label, card, badge, skeleton, dialog, dropdown-menu, tooltip, tabs,
               table, alert, toast (sonner), spinner, empty-state, error-state, stat, amount, address,
               sheet (mobile nav)
    shell/     app-shell, top-nav, mobile-nav, network-badge, connection-status, disclosure-banner,
               theme-toggle, user-menu
    wallet/    connect-button, sign-in-gate, wrong-network-dialog, session-expired-banner
    dashboard/ portfolio-card, energy-stats, earnings-card, open-orders-summary, recent-activity
  src/lib/
    amounts.ts                     the only place that formats/parses WATT / mUSDC / price
    config.ts                      env → typed config (chain id, API URL, WS URL, addresses from shared)
    chains.ts  wagmi.ts            anvil (31337) + sepolia (11155111) from config
    api/  schema.d.ts (generated)  client.ts (openapi-fetch, credentials include, error envelope → ApiError)
    auth/ siwe.ts  session.ts      nonce → EIP-4361 message → sign → login; /me poll; 401 → expired state
    realtime/ sync.ts  client.ts  hooks.ts   (see below)
    hooks/    use-balances, use-portfolio, use-market, use-network-guard
    store/    ui-store (zustand: theme-agnostic UI flags), connection-store
  tests/      Vitest + RTL; tests/e2e-visual/ Playwright screenshots
packages/shared/
  openapi.json                     committed, generated from FastAPI
  vectors/ws-sync.json             shared realtime scenarios (D2)
services/api/
  scripts/export_openapi.py        app.openapi() with no I/O → openapi.json
  app/routers/portfolio.py         (D1)
```

### Generated API client
`make gen-api`: `export_openapi.py` writes `packages/shared/openapi.json`, then `openapi-typescript` writes `apps/web/src/lib/api/schema.d.ts`; `openapi-fetch` provides the typed client. CI step runs `make gen-api` and fails on `git diff --exit-code`. The FastAPI app is built without Postgres/Redis for export (checked when implementing; if it needs them, the exporter builds the schema from the router list only).

### Realtime client (port of `ws_sync.py`)
- `sync.ts`: `ChannelSync<S, D>` with the same state machine and method names (`onMessage`, `onResync`, `loadSnapshot`, `resumePoint`) and the same `Outcome` values. Pure, no timers, no sockets.
- `client.ts`: `RealtimeClient` owns one socket: subscribe/unsubscribe with ids, `resume` built from each channel's `resumePoint`, buffering while syncing, snapshot fetch via a caller-supplied loader that returns `{data, epoch, seq}`, `resync` frame handling, application ping/pong with a 2-interval liveness timeout, exponential backoff 0.5 s → 30 s with full jitter, close-code policy (`4401` → emit `session-ended`, no blind retry; `4400`/`4413` → report as bug, stop; others → reconnect), `1001` → immediate reconnect. Injectable `WebSocket` factory, clock and RNG for tests.
- `hooks.ts`: `useChannel(channel, {loader, apply})` and `useConnectionStatus()`; a Zustand store feeds the status indicator (`connecting | live | resyncing | reconnecting | offline | session-ended`).
- Tests (Vitest, fake socket + fake timers) cover every Python scenario by running `vectors/ws-sync.json`, plus gateway-level cases: resume replay in order, resume at head, stale-epoch `resync`, outside-window `resync`, flush mid-session (epoch change on a live socket), gap → REST re-snapshot → continues, duplicate ignored, snapshot older than buffer → refetch, `4401` handling, backoff schedule and jitter bounds, heartbeat timeout reconnect.

### Amounts module (`lib/amounts.ts`)
bigint only. `WATT_DECIMALS = 3`, `USDC_DECIMALS = 6`; `formatWatt`, `formatKwh` (WATT ÷ 1000, exact), `formatUsdc`, `formatPrice` (per kWh), `parseWatt/parseUsdc/parsePrice` (strict: rejects extra decimals, negatives, exponents, empty; never `Number`/`parseFloat`), `costOf(fillWh, price)` and `feeOf(cost, feeBps)` mirroring the contract's floor rounding, locale-aware grouping through `Intl.NumberFormat` applied to the *string* part only. A lint rule bans `parseFloat`/`Number(` on amount-typed values under `src/`. API amounts arrive as decimal strings and are converted with `BigInt()` at the boundary. Property tests (fast-check) for format∘parse round-trip and ordering.

### Wallet and session
RainbowKit + wagmi config from `lib/chains.ts` (only the configured chain; transports from env RPC URL). Flow: connect → wrong-network detected (`useChainId` vs configured) → blocking dialog with one-click `switchChain` (and `wallet_addEthereumChain` fallback for Anvil) → "Sign in" (SIWE: `GET /auth/nonce`, EIP-4361 message built from config domain, `signMessage`, `POST /auth/login`) → session store. Session expiry: `/auth/me` revalidated on focus and on a timer ahead of `exp`; any 401 from the client or WS close `4401` flips to `expired`, shows a banner with "Sign in again", and stops authenticated queries (no retry storm). Account change or disconnect clears the session via logout. Typed errors for user-rejected signatures (shown as a calm inline message, not a toast storm).

### App shell
Top nav (Landing, Dashboard in 6a), mobile sheet nav at < 768 px, network badge (chain name + colour, "Anvil · local" / "Sepolia · testnet", amber when wrong network), connection status (from the realtime store, with tooltip detail), user menu (address, copy, explorer link when the chain has one, sign out), theme toggle, skip-to-content link. **Disclosure bar**: persistent, one line at 360 px, expandable: "Demo mode · testnet · simulated smart meters · mUSDC has no monetary value · no physical energy is delivered". Not dismissible permanently (collapse state only).

### Dashboard (real data only)
- Portfolio: WATT, mUSDC balances (viem `balanceOf`), exchange allowances, value of WATT at the live reference price (labelled "at reference price"), amount locked in open orders (from `/orders`).
- Energy: produced (confirmed mints), sold, bought (confirmed trades), pending/failed mint counts linked to "view" in 6b (shown as counts, no dead link in 6a).
- Earnings: gross sold, fees paid, net; period selector 24 h / 7 d / 30 d / all via `/portfolio` (D1).
- Each card has loading skeleton, empty state (explains how to get data: register a meter / place an order, as text in 6a), and error state with retry. Signed-out view prompts connect + sign-in. Live: balances refetch on `user:{address}` fills and `price:{market}` updates via the realtime client.

## Landing
Hero with the four product properties, how it works in 4 steps, honest-scope panel (testnet/simulated), live reference price tile and the 24 h price from `/markets` (real call; shows an error/empty state if the API is down), CTA to connect. No placeholder copy.

## /dev/ui — not in production builds
`next.config.mjs` sets `pageExtensions` to `["tsx","ts"]` by default and adds `"dev.tsx"` only when `WEB_DEV_UI=1`, so `app/dev/ui/page.dev.tsx` does not exist as a route in a normal `next build` (verified, not assumed: CI greps `.next/server/app` and the route manifest for `dev/ui`, and a Vitest asserts the config). The page renders each component in every state (default, hover/focus via forced-state classes, disabled, loading, error, empty, long text, RTL-safe truncation), both themes via the real toggle, plus token swatches, type scale, spacing/radius/elevation/motion demos and the contrast table. The Playwright script builds with `WEB_DEV_UI=1`.

## Visual QA script
`pnpm --filter @wattswap/web shots` (`make web-shots`): builds with `WEB_DEV_UI=1`, starts `next start`, uses Playwright Chromium to capture each page — Landing, Dashboard (signed out; signed in with data; empty user; API error; loading) and `/dev/ui` — at 360, 768 and 1440 px in dark and light, to `docs/screenshots/phase-6a/{page}-{width}-{theme}.png` (gitignored except the contact sheet kept for the PR). It also runs `@axe-core/playwright` per page/theme/width and fails on serious/critical violations, and checks no horizontal scroll at 360 px. I then review every image myself and fix what looks off before reporting.

## Tests (6a)
Vitest + RTL: amounts (incl. property tests), realtime sync + client (shared vectors + gateway scenarios), SIWE message builder, session state machine (expiry, 401, account switch), wrong-network dialog (renders, switches), network badge, disclosure bar, dashboard cards in loading/empty/error/data states, API client error mapping, token contrast pairs, dev-UI exclusion. Python: parametrised test over `ws-sync.json`; `/portfolio` unit + API tests. CI: `gen-api` drift check, `pnpm lint typecheck test`, production build with route-exclusion check.

## Dependencies to add (pinned after checking installed docs)
`wagmi@2`, `viem` (already via shared), `@rainbow-me/rainbowkit@2`, `@tanstack/react-query@5`, `zustand`, `siwe`-free hand-built EIP-4361 message (to avoid ethers pulled in by `siwe`; format checked against the API's verifier), `openapi-typescript`, `openapi-fetch`, `sonner`, `framer-motion`, `fast-check`, `@playwright/test`, `@axe-core/playwright`. RainbowKit needs a WalletConnect project id for WalletConnect; I will make it optional (injected wallets work without it) and document the env var.

## Risks
- RainbowKit/wagmi/React 19/Next 15 version friction → verify installed peer ranges first; fall back to wagmi connectors with a custom modal and write an ADR if RainbowKit cannot be made to work.
- SSR hydration mismatches from wallet state → wallet-dependent UI renders in client components behind a mounted guard with skeletons.
- Anvil is not in RainbowKit's default chains → custom chain object, tested.
- Playwright Chromium download (~150 MB) on this machine; Docker not required for screenshots.
- `next-themes` flash → set `suppressHydrationWarning` and the class strategy already used by the scaffold.
- API cookie is `SameSite=Lax`: fine for localhost; production hosting constraint is already recorded in `phase-8.md`.

## Acceptance criteria (6a)
1. `pnpm --filter @wattswap/web lint typecheck test build` green; `make gen-api` leaves a clean tree.
2. Python `test_sync_protocol.py` and Vitest both pass `ws-sync.json`.
3. `next build` without `WEB_DEV_UI` has no `/dev/ui` route; with it, the page renders.
4. Against the local stack: connect → switch network → SIWE → dashboard shows real balances/energy/earnings that match `cast`/DB for the demo users.
5. Session expiry (cookie cleared / logout elsewhere) shows the banner and stops queries.
6. Screenshots exist for every 6a page and `/dev/ui` × 3 widths × 2 themes; axe has no serious/critical violations; no horizontal scroll at 360 px; I have reviewed each image.
7. No `any`, no hex literals in components, no `parseFloat`/`Number` on amounts.

## 6b component list (page by page, for later)
- **Trade**: price chart (lightweight-charts, `price:` + `/ticks`), depth chart + order book ladder (`orderbook:`), trade tape (`trades:`), order form (limit buy/sell, kWh + price inputs via `parse*`, cost/fee preview from `costOf/feeOf`, balance and allowance checks), approve step with plain-language explanation, EIP-712 preview (human-readable fields next to the typed-data hash), open orders and fills tables (`user:`), cancel with explicit labels — **"Cancel (off-chain, instant, free)"** vs **"Cancel on-chain (gas; invalidates the signed order)"** with a status chip for each — and `dust_remainder` shown on partially filled orders with its explanation.
- **Meters**: register/view meters, live generation chart (`meter:`), mint history with status; **failed readings visible** with reason and attempts, not hidden behind confirmed ones; simulated badge on every simulated meter.
- **Insights**: weather/market overview, 24 h supply vs demand forecast (Recharts, `forecast:`), **live price-driver explanation** rendered from `price.drivers` (each term, the clamp/limiter that applied, the breaker state) in sentences.
- **Activity**: trades, mints, orders with explorer links and settlement state timeline.
- **Settings**: theme, display units, session info, notification toggles that persist (localStorage with fallback).
- **Admin** (role-gated): pricing parameters read/write, breaker status + reset, trading pause/resume, events log.
- Gate: Lighthouse a11y ≥ 90 on every page, Vitest for hooks and the order-signing flow, screenshot sweep extended to the new pages.

## Outcome
_To be filled in when 6a is implemented (what was built, how to verify, limitations)._
