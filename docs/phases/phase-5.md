# Phase 5 — WebSocket gateway

Branch: `phase-5-realtime` · Target tag: `v0.5.0` (after approval)

Status: **IMPLEMENTED — awaiting PR approval.** D1–D7 were approved with the amendments listed under *Owner changes*.

## Starting state (facts that shape the design)

- **Phase 4 did not build Redis streams.** `matching/events.py` and `pricing/publish.py` do `INCR {channel}:seq` and then `PUBLISH` (fire-and-forget pub/sub). Nothing is retained, so resume-from-seq needs a retained log that does not exist yet. This phase adds it (Redis Streams, below). This deviates from "build on the Phase 4 streams" only in that the streams are new; the envelope, channel names and `seq` counters are kept.
- **Two latent ordering bugs in the Phase 4 publisher** that the gateway would expose:
  1. `INCR` and `PUBLISH` are two round trips. Two concurrent publishers (matcher + listener + API process) can deliver seq 6 before seq 5, which a client would read as a gap and resync forever. Fix: one Lua script does INCR + XADD + PUBLISH atomically.
  2. `Outbox.flush` computes the book depth, *then* publishes. Two flushes can compute A then B but publish B then A, so the later seq carries the older book. Fix: compute and publish under a short per-market Redis lock.
- **Snapshots do not carry a seq today** except `GET /markets/{id}/price`. `/book`, `/trades`, `/orders` do not, and `/book` is served from a cache that is not tied to a seq. Fixed here.
- The `price` message already carries `drivers` (supply/demand terms, profile multiplier, smoothing, breaker) and `events`. The Insights page needs no new price channel for drivers. The supply-vs-demand **forecast** only lives behind `GET /markets/{id}/forecast` (written to Redis on each weather poll); see D5.
- The charter lists `meter:{meterId}` but nothing publishes meter data yet. Ingest (`routers/readings.py`) is the natural publisher.
- Auth is an httpOnly SIWE cookie (`wattswap_session`, JWT HS256 with `jti`; logout writes `jwt:deny:{jti}` to Redis). The WS handshake sees the same cookie. `websockets` is already in the lockfile (via uvicorn[standard]).
- Test infra: real Postgres/Redis/Anvil, `REQUIRE_SERVICES=1` in CI, `integration` marker.

## Scope

In: `/ws` endpoint, subscribe/unsubscribe, seq envelope, atomic publisher + retained streams, resume, snapshot+delta protocol on REST, Origin allow-list, user-channel auth, session-end closes across replicas, heartbeat, backpressure, limits and close codes, Redis fan-out, two-replica compose proof, `meter:` and `forecast:` publishers, integration + gap/resync tests, load test, docs.
Out: the browser client and connection-status UI (Phase 6; I will write the reference client logic as a tested Python helper and as pseudo-code in `docs/api.md`, not React), Playwright, Sepolia.

## Design

### Module layout (`services/api/app/`)

```
realtime/
  channels.py     parse/validate channel names; per-kind rules (public | user | meter)
  publisher.py    publish(redis, channel, type, data, retention) -> seq   [Lua: INCR+XADD+PUBLISH]
  streams.py      read_since(redis, channel, last_seq) -> list | ResyncRequired ; head_seq()
  hub.py          per-process: Redis pub/sub connection, ref-counted channel subscriptions, fan-out to connections
  connection.py   one socket: reader loop, bounded send queue + writer loop, heartbeat, limits, subscriptions
  protocol.py     Pydantic v2 models for every inbound/outbound message (discriminated unions) + close codes
  auth.py         handshake: Origin check, cookie -> claims, expiry timer, revocation listener
routers/ws.py     @router.websocket("/ws")
```

`matching/events.py::publish` and `pricing/publish.py` call `realtime.publisher` instead of their own INCR/PUBLISH. Channel names, envelope and payloads are unchanged, so Phase 4 tests keep passing.

### Envelope and messages

Server → client: `{type, channel, seq, ts, data}` for data (as today). Control frames (`subscribed`, `unsubscribed`, `resync`, `error`, `ping`) use `{type, ...}` with **no `seq`** and a `ref` echoing the client's request id.
Client → server: `{op: "subscribe"|"unsubscribe"|"ping"|"pong", id?, channels?: [...], resume?: {channel: lastSeq}}`.

Subscribe reply (`subscribed`) returns per channel `{channel, seq}` = the channel's head seq at the moment the subscription became live. With `resume`, the server replays retained messages with `seq > lastSeq` *before* live messages, in order, then sends `subscribed`; if `lastSeq` is older than the retained window or ahead of the head (Redis was flushed), it sends `{type:"resync", channel, reason, seq: head}` and no replay — the client must re-fetch the REST snapshot.

### Retention and resume (Redis Streams)

- Key `stream:{channel}`, entry id `{seq}-0` (the seq *is* the stream id, so `XRANGE` by seq is exact), one field `m` = the envelope JSON.
- Trimmed with `MINID` by age and `MAXLEN ~` by count: **DECISION D1** — public channels keep 10 minutes / 5 000 entries, `user:` channels 30 minutes / 1 000, `price:` 1 hour / 500 (ticks are 15 s). All env-configurable (`WS_RETAIN_*`). Memory is bounded by the count cap regardless of traffic.
- Subscribe-with-resume ordering (no gaps, no duplicates): `SUBSCRIBE` first and buffer live messages → `XRANGE (lastSeq +` → send replay → flush buffered live messages with `seq > last sent` → mark live. Dedup is by seq, so overlap is harmless.
- Replay cap: at most `WS_RESUME_MAX_MESSAGES` (default 1 000) per channel, otherwise `resync`.

### Snapshot + delta consistency

Protocol (written into `docs/api.md` with a sequence diagram):

1. Client subscribes → gets `subscribed{seq: S0}` (live stream starts after S0) and buffers deltas.
2. Client fetches the REST snapshot. **The server reads the channel head seq *before* reading the data** and returns `{seq: Ssnap, ...}`, so every change after Ssnap is guaranteed to be a delta with `seq > Ssnap`; changes between the read of seq and the read of data may be in both snapshot and delta (at-least-once).
3. Client applies only buffered deltas with `seq > Ssnap`; thereafter it requires `seq == last + 1`. A gap (`seq > last+1`) → discard buffer, re-fetch snapshot (or `resume` if inside retention). `seq <= last` is dropped as a duplicate.
4. **Deltas are idempotent by construction**: `orderbook` messages carry the full depth (state-replacing); `order`/`fill` upsert by `orderHash`/`tradeId`; `trade` dedupes by trade id; `price` replaces. That is what makes at-least-once safe, and it is stated as a rule in the docs.

REST changes (additive; existing fields untouched):
- `GET /markets/{id}/book` → adds `seq`. Served from the cache only if the cached body's seq equals the current head, else rebuilt (the cache body gains `seq`).
- `GET /markets/{id}/trades` → response stays a list for compatibility; seq returned in header `X-Channel-Seq` (**DECISION D2**: header rather than envelope-wrapping the list, to avoid breaking the Phase 4 response shape and its tests. Alternative: a new `/snapshot` object endpoint. Tell me if you prefer that).
- `GET /orders`, `GET /trades` (mine) → `X-Channel-Seq` for `user:{me}`.
- `GET /markets/{id}/price` already returns `seq`.

### Security

- **Origin allow-list on the handshake**, checked *before* `accept` (reject with HTTP 403, close code 1008 semantics). Allowed = `API_CORS_ORIGINS` plus optional `WS_EXTRA_ORIGINS`. **DECISION D3:** a missing `Origin` header (non-browser clients such as wscat/curl-style tools) is allowed only when `WS_ALLOW_MISSING_ORIGIN=true`; default `true` in development, `false` in production (Settings validator, like the JWT secret check). A present Origin that is not listed is always refused. Note: cookie `SameSite=Lax` is not relied on.
- **Auth**: cookie → `decode_token` + `is_revoked` at handshake. Anonymous sockets are allowed for public channels only. Subscribing to `user:{address}` requires `address == claims.address` (case-insensitive); anything else gets an `error{code:"forbidden"}` and never joins the Redis subscription. `meter:{id}` requires the meter's owner (DB lookup at subscribe).
- **Session end closes sockets**: each authenticated connection schedules `call_at(exp)` → close **4401 `session_expired`**. Logout publishes `{jti, address}` on the Redis pub/sub channel `ws:control`; every replica's hub closes that jti's sockets with **4401 `session_revoked`** (a change to `routers/auth.py::logout`, one line). Also, `user:` subscribe-time check and a periodic (30 s) denylist re-check as a backstop in case a control message is lost (pub/sub is lossy).
- Inbound validation: Pydantic models with `extra="forbid"`; channel strings matched against strict regexes (`0x` + 64 hex market ids, 40-hex addresses); nothing from a client is ever used to build a Redis key before validation.
- Server never echoes other users' data: `orderbook` carries no addresses (Phase 4 already guaranteed this), `trades:` carries buyer/seller (already public on-chain).
- Redis streams for `user:` channels hold private order data — Redis is inside the trust boundary, but it goes into the threat model.

### Limits and close codes (documented in `docs/api.md`, all env-configurable)

| Limit | Default | Behaviour |
|---|---|---|
| Subscriptions per connection | 20 | `error{code:"too_many_subscriptions"}`, socket stays open |
| Inbound frame size | 4 KiB | close **4413** |
| Inbound rate | 20 msgs / 10 s sliding, burst 40 | first breach → `error{code:"rate_limited"}`; sustained (3 breaches) → close **4429** |
| Connections per IP / per address | 20 / 10 | handshake refused HTTP 429 (counted in Redis so it holds across replicas) |
| Send queue | 256 messages | overflow → close **4409 `slow_consumer`** (client reconnects and resumes) |
| Heartbeat | server `ping` every 20 s; any inbound frame counts; no frame within 2 intervals | close **4408 `heartbeat_timeout`** |
| Handshake not completed (no first frame needed — only applies to bad framing) | – | – |

Close codes: 1000 normal · 1001 server shutdown (graceful drain on SIGTERM) · 1008 handshake policy (Origin, pre-accept: HTTP 403) · 4400 malformed JSON/unknown op · 4401 session expired/revoked · 4408 heartbeat timeout · 4409 slow consumer · 4413 message too large · 4429 rate limited. Per-request failures (unknown channel, forbidden channel, subscription cap) are `error` frames, not closes; protocol abuse is a close.
**DECISION D4:** application-level JSON ping/pong rather than WebSocket control-frame pings, because browsers cannot send or observe control-frame pings, and it is testable with wscat. uvicorn's own protocol ping stays at its default.

### Fan-out across replicas

- Each process runs one `Hub` with one Redis pub/sub connection. A channel is `SUBSCRIBE`d when its first local subscriber arrives and `UNSUBSCRIBE`d when the last leaves (ref-counted), so a replica only receives what its clients want.
- Publishers (matcher, listener, pricing, ingest) may be in any process; Redis is the bus. The gateway never trusts local state for ordering — it forwards by the envelope's `seq`.
- Serialization: the envelope JSON is received once from Redis and the same string is enqueued to every local subscriber (no per-client re-encode).
- Hub reconnect: if the Redis pub/sub connection drops, the hub reconnects with backoff, re-subscribes, and sends each affected channel a `resync` (clients resume from their last seq).
- **Compose proof (D6):** add `api2` (same image, port `${API2_PORT:-8001}`, `PRICING_EMBEDDED=false`, `TRADING_EMBEDDED=false` so only `api` runs the workers; `api2` is a pure gateway/REST replica). `make ws-fanout-demo` starts both and runs `dev/ws_fanout_demo.py`: client A on `:8000`, client B on `:8001`, both subscribed to `orderbook:{market}`; an order is placed through `:8001`, and the script asserts that both clients get the same `seq` sequence and prints the observed order. It must pass against the real compose stack, and the same scenario also runs as a pytest integration test with two in-process apps against one Redis.
- Alembic runs in the `api` CMD; `api2` must not migrate concurrently → `api2` gets `command` without `alembic upgrade` and `depends_on: api (healthy)`.

### Price drivers / forecast channel

- `price:{market}` already carries `drivers` and `events`; this phase types and documents them in the WS schema (a `PriceMessage` model with the documented `drivers` keys), and a test asserts a live tick delivers `drivers.supply.*`, `drivers.demand.*`, `drivers.breaker`.
- **DECISION D5:** add channel `forecast:{market}` (type `forecast`), published when the weather poll writes `pricing:forecast:{market}` (every 5–10 min), because supply-vs-demand forecast charts otherwise need polling. One publish call in `pricing/service.py`. Say if you would rather drop it.

### `meter:{meterId}`

`routers/readings.py`/`services/readings.py` publishes `reading` (accepted, with kWh interval and timestamps) and the mint worker publishes `mint` status changes (`pending → confirmed`). Owner-only subscribe. Meters are the labeled simulator; messages carry the existing `simulated` marker so the UI cannot present them as hardware.

### Observability

Prometheus: `ws_connections` (gauge), `ws_connections_total{result}`, `ws_closes_total{code}`, `ws_subscriptions` (gauge), `ws_messages_out_total{type}`, `ws_send_queue_depth` (histogram), `ws_delivery_latency_seconds` (envelope `ts` → socket write, histogram), `ws_resume_total{outcome=replayed|resync}`, `realtime_publish_total{channel_kind}`. Structured logs carry connection id, address (truncated) and close code.

## Tests

**Unit (no services):** channel parser (valid/invalid/injection strings), protocol models (`extra=forbid`, size), rate limiter, backpressure queue, sequence-gap detector, reference client state machine.

**Integration (real Redis + Postgres, in-process ASGI app, real WebSocket client via `websockets`):**
1. subscribe/unsubscribe, envelope shape, `subscribed` seq.
2. user channel: own address OK; other address → `forbidden`, and no data leaks when the other user's order is placed; anonymous → public only.
3. Origin: listed OK; foreign Origin → 403 before accept; missing Origin per setting.
4. Session end: logout closes the socket with 4401 (including on a *second* app instance); JWT expiry closes with 4401 (short-TTL token, clock-driven).
5. Heartbeat: silent client closed 4408 (shortened interval); pong keeps it alive.
6. Backpressure: client that never reads → 4409, and other clients are unaffected.
7. Limits: oversize frame 4413, rate 4429, subscription cap error, malformed 4400.
8. **Gap → resync path:** drop a message in transit (Redis-level publish that skips a seq) → reference client detects the gap, re-fetches the REST snapshot, and its final book equals the server's `GET /book`. Plus the stale-snapshot case (delta `seq <= snapshotSeq` is ignored).
9. **Resume:** disconnect, N events happen, reconnect with `resume` → exact missed messages, in order, no duplicates; resume outside the retention window → `resync`; resume ahead of head (Redis flushed) → `resync`.
10. Atomic publisher: 50 concurrent publishers to one channel → seqs strictly 1..50 on the wire in order (this fails against the Phase 4 publisher; it is the regression test for the ordering bug).
11. Orderbook publish order: concurrent flushes never publish an older book under a newer seq.
12. **Two replicas:** both apps on one Redis; a client on each; trade placed via app 2 → both see identical seq sequences. Replica B closes a user's sockets when logout happens on replica A.
13. Price channel: a driven tick delivers `drivers`; `forecast` message on weather refresh; `meter:` owner-only.
14. End-to-end (Anvil): two wallets cross → the `user:` and `trades:` channels show `order → fill(matched) → fill(confirmed)` and `trade`, in seq order.

**Load test** (`services/api/loadtest/ws_load.py`, `make ws-load`): N=500 concurrent clients (asyncio, one process, `websockets`) subscribed to one market's `orderbook`+`price`+`trades`; a publisher emits M messages at a fixed rate; each message carries a monotonic send timestamp so delivery latency is `recv − envelope ts_ns` (a dedicated high-resolution field used only by the load tool, since envelope `ts` is whole seconds). Reports connect time, messages delivered/expected (loss must be 0), p50/p95/p99 latency, per-connection queue high-water, API CPU/RSS. Runs against one replica and then against two (clients split 250/250). **Honesty note:** client and server share this one machine, so absolute numbers are indicative; I will report the machine and say so. Acceptance is defined by *loss = 0 and p95 below a documented budget (proposed: 250 ms)*, which I will tune only by reporting the measured number, not by moving the goalposts quietly. A reduced run (100 clients) is part of CI; the 500-client run is a `make` target and its result goes into the phase report.

## Files (high level)

New: `app/realtime/*`, `app/routers/ws.py`, `dev/ws_fanout_demo.py`, `services/api/loadtest/ws_load.py`, `tests/unit/test_ws_*.py`, `tests/integration/test_ws_*.py`, `docs/adr/009-realtime-gateway.md` (streams as the retained log, atomic Lua publisher, app-level heartbeat, Origin policy, header-carried snapshot seq), `docs/phases/phase-5.md` (this, updated at the end).
Changed: `matching/events.py`, `pricing/publish.py`, `pricing/service.py`, `routers/{orders,auth,readings}.py`, `services/readings.py`, `workers/mint_worker.py`, `schemas/trading.py` (+`seq`), `core/config.py` (+`ws_*` settings, production validators), `core/metrics.py`, `main.py`, `docker-compose.yml` (+`api2`), `.env.example`, `Makefile`, `.github/workflows/ci.yml`, `docs/api.md` (+WebSocket section: message schemas, examples, protocol, sequence diagram, close codes), `docs/threat-model.md` (+cross-site WS hijack, slow-loris/slow consumer, stream data exposure), `docs/architecture.md`, `README.md`.
No contract, migration or `packages/shared` changes expected.

## Risks

| Risk | Mitigation |
|---|---|
| Lua publish script blocks Redis | Tiny script (INCR, XADD with `MAXLEN ~`, PUBLISH, optional SET); O(1) amortised. Loaded with `register_script` (EVALSHA). |
| Redis pub/sub is lossy (replica reconnect, control messages) | Data: gap detection + resume. Control (logout): backstop denylist poll every 30 s. |
| Book publish lock adds latency/contention to the matcher | Lock is per market, held only for one read query + one Lua call; on lock timeout the publish is skipped and a later flush carries the latest state (book messages are state-replacing, so skipping is safe) — but the seq must stay gapless, so the skip happens *before* a seq is assigned. |
| Cookie auth + browsers on another origin/port | Origin allow-list covers it; documented dev setup `localhost:3000` → `localhost:8000`. |
| Fixed-window `hit()` limiter is the wrong shape for message rate | Per-connection in-memory sliding window for messages; Redis counters only for connection counts. |
| Load result on a laptop is noisy | Reported with machine details and as an indicative figure; loss=0 is the hard gate. |
| Windows dev host | Compose and tests run the same as Phase 4 (Docker + Git Bash). The load test runs on the host against the compose stack. |

## Acceptance criteria

1. `wscat -c ws://localhost:8000/ws` can subscribe to `price:{market}` and receives seq-numbered envelopes; `make ws-fanout-demo` passes against `api` + `api2` (shared seq, no gap/dup).
2. All integration tests above pass with `REQUIRE_SERVICES=1`; the Phase 1–4 suites are still green (publisher change is behaviour-preserving).
3. Gap → resync and resume paths are tested and documented, including out-of-window and flushed-Redis cases.
4. Foreign Origin refused; user channel for another address refused; logout and JWT expiry close sockets on every replica.
5. Every limit has a test and a documented close code.
6. `make ws-load` with 500 clients: zero lost messages; p95 reported and within budget (or reported honestly if not, with the cause).
7. `ruff`, `ruff format --check`, `mypy --strict` clean; CI updated (ws tests + reduced load test + compose two-replica smoke if CI time allows, otherwise the pytest two-app test is the CI gate and the compose proof is run locally and recorded here).
8. `docs/api.md` WebSocket section with schemas, examples, sequence diagram; ADR 009; this file updated with what was built and known limitations.

## Decisions awaiting your confirmation

- **D1** retention windows (10 min / 5 000 public; 30 min / 1 000 user; 1 h price).
- **D2** snapshot seq for list endpoints via `X-Channel-Seq` header (book gets `seq` in the body).
- **D3** missing-Origin policy: allowed in dev, refused in production, configurable.
- **D4** app-level JSON heartbeat instead of control-frame pings.
- **D5** extra `forecast:{market}` channel for the Insights page.
- **D6** second replica added to compose as `api2` (gateway-only; workers stay on `api`).
- **D7** cookie-only WS auth (no token in query string or first message, so tokens never land in URLs or logs).

## Order of work (small commits, tests after each)

1. `realtime/publisher.py` + `streams.py` + Lua, swap Phase 4/3 publishers, ordering regression tests (commit: `feat(realtime): atomic publisher with retained streams`).
2. Book publish serialization + `seq` on snapshots.
3. `protocol.py`, `channels.py`, unit tests.
4. Hub + connection + `/ws` (subscribe/unsubscribe, heartbeat, limits, backpressure).
5. Auth: Origin, user channels, expiry, logout broadcast.
6. Resume + resync, gap tests with the reference client.
7. `meter:` and `forecast:` publishers; price schema test.
8. Two-replica pytest, compose `api2`, `ws-fanout-demo`.
9. Load test + metrics.
10. Docs, ADR, threat model, README, CI; full lint/test run; PR; §9 report.

## Owner changes (applied)

- D2: `X-Channel-Seq` is in `Access-Control-Expose-Headers`; tested (`test_snapshot_seq_header_is_readable_cross_origin`) at the HTTP level: allow-origin + allow-credentials + expose-headers present for the web origin, absent for a foreign origin. **No real browser was driven** (Playwright is Phase 7).
- D3: missing Origin allowed only when the environment setting was explicitly `development`. The setting in this codebase is `API_ENV` (not `APP_ENV`); "unset" is detected through pydantic `model_fields_set`, because the field's default is `development`. Tested for unset / development / test / production, from kwargs and from the environment.
- D7: ADR-009 "Deployment requirement" + `docs/phases/phase-8.md` checklist (new file). Added `SESSION_COOKIE_DOMAIN` (default host-only) so the Domain is explicit and tested.
- Lua script keys are hash-tagged per channel (`{chan:<channel>}:seq|stream|latest`).
- Concurrent-publisher regression test: `test_concurrent_publishers_deliver_strictly_increasing_seq_without_gaps` (500 messages from 20 connections arrive as exactly 1..500) plus `test_old_publisher_pattern_does_reorder`, which shows the Phase 4 INCR-then-PUBLISH pattern does reorder.

## Outcome

Built as planned, with these deviations: retention by count uses exact `MAXLEN` (approximate trimming kept ~100 extra entries, making the window unpredictable); the per-send `wait_for` timer was replaced by stall detection (see load results); the `/ws` connection cap is enforced after the upgrade (close `4429`) because a pre-upgrade refusal can only be HTTP 403; the `ws.close` code for Origin failure is therefore HTTP 403, not a WebSocket close code.

Also fixed along the way: `MintWorker._resolve` fell through after confirming a mint and confirmed it a second time (a Phase 2 bug, found because it produced a duplicate `mint` message); the funding of `book` snapshots now carries `seq`.

### Verification

| Check | Result |
|---|---|
| Full API suite (`REQUIRE_SERVICES=1 uv run pytest`) | 492 passed, 12 m 15 s |
| `tests/realtime` | 55 tests: publisher, gateway, auth/Origin/CORS, sync + resume + gap, replicas, streams, load smoke |
| `ruff check`, `ruff format --check`, `mypy --strict` (app, tests, dev, loadtest, alembic) | clean |
| `make ws-fanout-demo` (docker compose, `api` + `api2`) | PASS: trade driven through replica 2; clients on :8000 and :8001 saw identical `(channel, seq)` sequences |

### Load test (`make ws-load`, 500 clients, one market, 1 500 B messages, 20 s)

Machine: Windows 11 host, Docker Desktop, 22 logical cores, client + Redis + gateways + publisher all on it. Clients are Python `websockets`; loop-lag was ≤ 45 ms max, so the numbers are not dominated by the tool. Indicative only.

| Topology | Publish rate | Deliveries/s | p50 | p95 | p99 | Lost / dup / out-of-order |
|---|---|---|---|---|---|---|
| 1 replica | 2 msg/s | ~1 000 | 60 ms | **149 ms** | 212 ms | 0 / 0 / 0 |
| 1 replica | 10 msg/s | ~5 000 | 48 ms | **437 ms (over the 250 ms budget)** | 597 ms | 0 / 0 / 0 |
| 2 replicas (250 each) | 10 msg/s | ~5 000 | 51 ms | **148 ms** | 181 ms | 0 / 0 / 0 |

Reading: a single gateway fans out ~4–5 k frames/s before queueing delay shows; delivery is lossless and ordered at every rate. The 250 ms p95 budget holds for one replica up to roughly 5 msg/s to 500 clients and for two replicas at 10 msg/s. Real market traffic (a tick every 15 s plus order-book changes) is well under the first case. Tuning done: `--ws websockets-sansio` in the image, no per-send timer, per-frame metrics batched (single replica at 10 msg/s went from ~780 ms to ~380–440 ms p95 across runs).

## Known limitations / tech debt

- The stream epoch covers loss of a channel's whole state (flush/restart); Redis must not evict individual keys (`noeviction`, Phase 8 checklist).
- Per-IP connection cap uses the socket peer address; behind a reverse proxy every client shares one IP (Phase 8 checklist).
- Single gateway process is CPU-bound near 5 k frames/s; scale by replicas. Per-socket message coalescing is not implemented (book messages are state-replacing, so it would be safe to add).
- Heartbeat liveness for a client that only reads is its `pong` replies; a client must answer pings.
- CORS exposure of `X-Channel-Seq` verified by headers, not by a browser.

## Pre-merge changes (second review)

1. **Stream epoch.** Per-channel random epoch (created with the channel state, recreated after loss) in every envelope, in `subscribed`/`resync`, in REST snapshots (`epoch` body field / `X-Channel-Epoch` header, exposed via CORS) and in every `resume` entry (`{epoch, seq}`; a bare seq is now a protocol error). A mismatch answers `resync: epoch_changed`; a *connected* socket gets the same frame before the first message of a new epoch. `dev/ws_sync.py`, `docs/api.md`, ADR-009 and threat model (W-10) updated. Test: `test_redis_flush_mid_session_is_detected_and_the_client_resyncs` flushes Redis under a live socket and shows the client detecting the change and re-snapshotting (plus unit tests that a new epoch is never mistaken for duplicates, and flush-while-disconnected).
2. **`API_ENV` defaults to `production`.** The `model_fields_set` special case is gone. Set to `development` explicitly in `.env.example`, docker-compose (literal, not `${API_ENV:-development}`) and the Makefile (host-side tools); the test suite sets `test` in `tests/conftest.py`. `test_unset_api_env_behaves_as_production_everywhere_it_is_read` covers every reader: config validation (dev JWT secret refused), the loadtest channel, the `/ws` missing-Origin policy, `_build_chain`, `build_pricing`, and `pricing.run`.
3. **Load tool uses a dedicated channel** `loadtest:run-<timestamp>` (new dev-only kind, `WS_LOADTEST_ENABLED`, refused when `API_ENV=production`, keys deleted afterwards). No real market channel is written to.
4. **Serialization audit.** The envelope is JSON-serialized once (in the publish script). The hub parses it once per message to read `(epoch, seq)` and passes the *same string object* to every client; nothing is encoded per client in our code (`test_one_parse_and_one_shared_string_for_all_clients` counts one parse and one shared object for 50 clients). The WebSocket library still UTF-8-encodes and frames the text per socket; that cannot be shared without switching to binary frames. So there was nothing to fix, but the load test was re-run because the hot path changed (epoch parse):

| Topology (500 clients, 10 msg/s, 20 s) | p50 | p95 | Lost |
|---|---|---|---|
| 1 replica, three runs | 50–56 ms | **272 / 353 / 424 ms** (over the 250 ms budget) | 0 |
| 2 replicas | 52 ms | **155 ms** | 0 |

The epoch added no measurable cost; the single-replica result is unchanged within run-to-run noise (earlier: 437 ms).
