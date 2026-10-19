# ADR-010: Frontend foundation (Phase 6a)

Status: accepted (the owner approved the plan and its three recommendations).

## Decisions

1. **`GET /portfolio` is computed in SQL**, not summed in the browser. The list endpoints are paginated (`/trades` is capped
   at 200 rows), so client-side sums silently under-report for active users. Definitions are in `docs/api.md`; a test seeds
   more rows than any page and compares against the fully paginated lists. `/trades` gained a `before` cursor so that
   comparison is possible.
2. **One shared scenario file for the realtime state machine**: `packages/shared/vectors/ws-sync.json`. The Python reference
   (`dev/ws_sync.py`) and the TypeScript port (`apps/web/src/lib/realtime/sync.ts`) both execute it, and CI runs both
   suites. Transport behaviour (resume, resync frame, backoff, heartbeat, close codes) is covered by gateway-level Vitest
   cases with a fake socket and a fake clock.
3. **Hand-built EIP-4361 message** instead of the `siwe` npm package (which depends on ethers). Correctness is enforced by a
   cross-language gate: the builder's output for seven cases is written to `packages/shared/vectors/siwe-frontend.json`
   (committed; Vitest fails if regenerating gives a different file) and the backend test parses it with the same Python
   `siwe` library that production login uses, re-serialises it to identical bytes and verifies every signature.
4. **`/dev/ui` is absent from production builds**, not hidden: its file is `page.dev.tsx` and `next.config.mjs` registers the
   `dev.tsx` page extension only when `WEB_DEV_UI=1`. A Vitest asserts the config; CI asserts the built route manifest.
5. **The test wallet uses Anvil's public dev keys only.** `createTestWallet` throws unless the app is configured for chain
   31337, and every signing method re-checks the wallet's current chain. It lives under `tests/`, never `src/`.
6. **Session expiry** is detected by any 401 from a data endpoint, a re-check of `/auth/me` every 60 s and on window focus, and
   WebSocket close `4401`. It moves the session to `expired` (banner, authenticated queries stop) instead of retrying.
7. **No polling for trade-dependent figures.** The dashboard subscribes to `user:{address}` and, on any event (and after any
   re-snapshot), refreshes portfolio totals and balances once, debounced by 400 ms.
8. **Fonts**: Geist and Geist Mono via `next/font` (kept from the Phase 0 scaffold) instead of the Inter + JetBrains Mono named
   in the plan. The scale and tokens are unchanged.
9. **Removed the `cn` npm package** the scaffold used for the class-name helper (an unrelated third-party package). The helper
   is now `clsx` + `tailwind-merge` in `src/lib/utils.ts`.

## Consequences and limits

- Mint status changes are published to `user:{owner}` as well as `meter:{id}` (same body), so `produced` on the dashboard
  updates live on every mint without needing `meter:` channels (which have no REST snapshot yet).
- Every paginated list uses an opaque keyset cursor on `(sort key, id)` (`app/core/pagination.py`), added after review:
  the first version used `created_at` alone and could skip rows that share a timestamp at a page boundary.
- The price endpoint returns an open-ended object; the client reads only the fields it uses, through a strict runtime parser.
- The production build prints a webpack "critical dependency" warning from a third-party package inside wagmi/RainbowKit
  (an expression in a `require`); it does not affect behaviour. The unresolvable optional `@x402/*` peers of Coinbase's SDK
  (pulled in by RainbowKit's connector barrel) are stubbed in `next.config.mjs`.
