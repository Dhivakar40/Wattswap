# API reference (Phases 2–5)

Base path `/api/v1`. Interactive docs and the OpenAPI schema: `GET /docs`, `GET /openapi.json`.
Operational endpoints sit at the root: `GET /health` (liveness), `GET /ready` (Postgres, Redis, RPC and the
on-chain role wiring), `GET /metrics` (Prometheus).

> Testnet demo. Meters are **simulated**; WATT does not represent physically delivered energy.

## Conventions

- **Errors** always use one envelope, with the request id also in the `x-request-id` response header:
  ```json
  {"error": {"code": "implausible_reading", "message": "…", "details": {"max_wh": 100}, "request_id": "…"}}
  ```
- **Authentication**: a session cookie `wattswap_session` (JWT, 12 h, `HttpOnly; Secure; SameSite=Lax`). Obtained
  with Sign-In with Ethereum. `POST /readings` is the exception: it is authenticated by the **device signature**.
- **Rate limits** (Redis, fixed window, `Retry-After` on 429): per IP, per user, per auth endpoint, per meter on ingest.
- **Identifiers**: addresses are returned EIP-55 checksummed and accepted in any case; `bytes32` values are
  lowercase `0x` hex; energy is integer **Wh** (`1000 Wh = 1.000 WATT = 1 kWh`).
- **CORS** is limited to `API_CORS_ORIGINS`; credentials are allowed for those origins only.

## Auth

| Method & path | Notes |
|---|---|
| `GET /auth/nonce` | `{nonce, expires_in}`. Single use, 5 min, stored in Redis. |
| `POST /auth/login` | Body `{message, signature}` (EIP-4361 text + EIP-191 signature). Requires the configured `SIWE_DOMAIN`, the configured chain id, `issued_at` within 10 min, and an `expiration_time`. EOA and EIP-1271 wallets. Sets the cookie. Returns `{id, address, role}`. |
| `POST /auth/logout` | Adds the token's `jti` to the Redis denylist until it would have expired and clears the cookie. The same cookie is refused afterwards. |
| `GET /auth/me` | The current user. `role` is `admin` if the address is in `ADMIN_ADDRESSES`. |

Message template (what the web app and the simulator's dev provisioning produce):

```
localhost:3000 wants you to sign in with your Ethereum account:
0xYourChecksummedAddress

Sign in to WattSwap (testnet demo).

URI: http://localhost:3000
Version: 1
Chain ID: 31337
Nonce: <from /auth/nonce>
Issued At: 2026-10-18T12:00:00.000Z
Expiration Time: 2026-10-18T12:05:00.000Z
```

## Meters

| Method & path | Notes |
|---|---|
| `POST /meters` | Body `{label, device_signer, rated_capacity_w, lat, lon}`. The user supplies only the device **address**; the device key never reaches the API. The API registers the meter on-chain with the registrar key and returns once it is confirmed (`register_status: confirmed`). `502 registration_failed` if the chain rejects it. |
| `GET /meters` | Own meters (admins: all). Includes `minted_wh`. |
| `GET /meters/{id}` | `{id}` is the uuid or the `bytes32` meter id. Someone else's meter is `404`, not `403`. |
| `PATCH /meters/{id}` | Any of `label`, `active` (mirrored on-chain with `setActive`), `device_signer` (key rotation, on-chain `rotateDeviceSigner`; the old key stops working immediately). |
| `GET /meters/{id}/readings?limit&before` | Newest first, each with its mint status. Page with `next_before`. |
| `GET /mints?limit&before` | Mint history for the caller's meters with `status`, `tx_hash`, `block_number`, `failure_reason`. |

## Ingest

`POST /readings`: body is the signed reading.

```json
{
  "meter_id": "0x…32 bytes", "reading_id": "0x…32 bytes",
  "amount_wh": 1200, "interval_start": 1799999100, "interval_seconds": 900,
  "signature": "0x…65 bytes", "simulated": true
}
```

- `reading_id` **must** be `keccak256(abi.encode(bytes32 meterId, uint256 intervalStart, uint256 intervalSeconds))`,
  so one interval has exactly one id and retries are naturally idempotent.
- `signature` is the EIP-712 `Reading` signature by the meter's device key over
  `{meterId, readingId, amountWh, intervalStart, intervalSeconds}` in the `WattSwap Energy` / version `1` domain of
  the `EnergyToken` contract (`GET /config` returns chain id and token address). `simulated` is informational and not signed.
- Response `202` (new, mint queued) or `200` (`duplicate: true`, nothing queued). Safe to retry.

| Status / code | Meaning |
|---|---|
| `404 unknown_meter` | No such meter |
| `409 meter_not_registered` / `meter_inactive` | Registration not confirmed, or deactivated |
| `422 reading_id_mismatch` | `reading_id` is not the derived id |
| `401 invalid_signature` | Not signed by the meter's current device key |
| `409 reading_conflict` | Same `reading_id`, different data |
| `422 invalid_interval` | Length outside the registry's `[minInterval, maxInterval]` |
| `422 interval_not_complete` | The interval has not ended yet |
| `422 implausible_reading` | `amount_wh` exceeds `capacity × the reading's own interval × safety factor`; `details.max_wh` says how much |
| `409 interval_out_of_order` | Starts before the meter's last accepted interval ended (overlap or out of order). Offline devices must backfill oldest-first. |
| `429 rate_limited` | Too many requests |

Zero-energy intervals (night) are not sent: nothing to tokenize, and the contract rejects 0 Wh.

## Mint lifecycle

`pending → submitted → confirmed | failed`

| Status | Meaning |
|---|---|
| `pending` | Accepted and queued |
| `submitted` | Signed and persisted, broadcast; waiting for `MINT_CONFIRMATIONS` |
| `confirmed` | `ReadingMinted` is on-chain; `tx_hash` / `block_number` set |
| `failed` | Reverted or rejected; `failure_reason` holds the decoded custom error, e.g. `MeterInactive(meterId=0x…)` |

## Config

`GET /config`: chain id, `EnergyToken` and `MeterRegistry` addresses, default `reading_interval_seconds`, the
registry's interval bounds and `safety_bps`, `simulated_meters: true`, and a note stating this is a testnet demo.

# Pricing (Phase 3)

Model, formulas and parameters: [pricing-model.md](pricing-model.md). Prices are integers in **mUSDC base units
(6 decimals) per kWh**; energy is in **Wh**.

## Public

| Endpoint | Notes |
|---|---|
| `GET /markets` | Markets with their region, `is_primary`, and `onchain_enabled` (`WattSwapExchange.marketEnabled`). |
| `GET /markets/{id}/price` | Latest tick: `price`, `rawPrice`, `breaker`, `bandBps`, supply/demand in watts, `marketTime`, `clockMode`, `accelerated`, the **signed quote** `{marketId, price, timestamp, signature, signer}` and the full `drivers` breakdown. `seq` is the Redis sequence number. 404 `no_price_yet` before the first tick. |
| `GET /markets/{id}/ticks?limit&before&drivers` | History, newest first; page with `next_before`; `drivers=true` includes the breakdown. |
| `GET /markets/{id}/forecast` | Next 24 hours of hourly generation, supply, demand and implied **raw** price (before smoothing). |

## Admin (role-gated; `ADMIN_ADDRESSES`)

| Endpoint | Notes |
|---|---|
| `GET /admin/pricing/config?market_id=` | Every parameter with kind, unit, default, effective value, `source` (default / global / market), bounds, `editable`, last change. |
| `PUT /admin/pricing/config` | `{market_id?, values: {...}, reset: [...]}`. Validated atomically (types, ranges, cross-parameter rules including the limiter/breaker rule); decimals as strings, no JSON floats; audited. Effective within seconds. |
| `GET /admin/pricing/state?market_id=` | Breaker (mode, reason, since), per-cell weather age and source, last weather error, last oracle push. Written by the engine, so it works when the engine is a separate process. |
| `POST /admin/pricing/breaker/reset` | Queues a manual reset (202); applied on the next tick. |
| `GET /admin/pricing/events?market_id=` | Breaker transitions and anomaly events, newest first. |
| `POST /admin/markets`, `PATCH /admin/markets/{id}` | Define a market (id = `keccak256(name)`), its bounding box, `enabled`, `is_primary` (only one). The exchange admin must still `setMarket` on-chain. |

## Redis feed

Channel `price:{marketId}` carries `{"type":"price","channel":"price:{marketId}","seq":N,"ts":T,"data":{...}}`
(`data` = the object returned by `/price`). `seq` increments per tick; a gap means re-snapshot via REST.
Snapshots: `price:{marketId}:latest`, `quote:{marketId}:latest`, `pricing:state:{marketId}`,
`pricing:forecast:{marketId}`.

# Trading (Phase 4)

Amounts and prices are decimal **strings** (uint256 values do not fit JSON numbers): energy in Wh, prices in
mUSDC base units per kWh. Sign orders with EIP-712 using the domain and types from `order-config`.

## Public

| Endpoint | Notes |
|---|---|
| `GET /markets/{id}/order-config` | EIP-712 domain/types, exchange/WATT/mUSDC addresses, `chain_id`, minimum sizes, price and expiry bounds, live `band_bps`/`fee_bps`, and ABI fragments of `cancel`/`cancelUpTo` for the binding on-chain cancel. |
| `GET /markets/{id}/book?depth=` | Aggregated levels `{price, amount_wh, orders}` for bids and asks (no addresses). Matchable Wh only (unreserved, not cancel-pending, unexpired). |
| `GET /markets/{id}/trades?limit&before` | The tape: **confirmed** trades only. |

## Authenticated (SIWE cookie)

| Endpoint | Notes |
|---|---|
| `POST /orders` | Body = the signed `Order` (`maker, marketId, side 0/1, amountWh, limitPrice, expiry, nonce, salt`) + `signature`. `maker` must be the signed-in address. **201** new, **200** if the same signed order was already stored. Error `code`s: `bad_signature`, `maker_mismatch`, `bad_order`, `order_too_small`, `bad_price`, `order_expired`, `bad_expiry`, `market_disabled`, `nonce_too_low`, `order_cancelled`, `already_filled`, `insufficient_balance` / `insufficient_allowance` (details: `required`, `already_committed`, `balance`, `allowance`, `block`), `too_many_open_orders` (429), `rate_limited` (429, `Retry-After`). |
| `GET /orders?status&market&limit&before_seq` | Your orders, newest first. |
| `GET /orders/{hash}` | One of your orders. |
| `DELETE /orders/{hash}` | **Soft, off-chain cancel.** Returns `{order, cancel_type: "offchain", cancelled_wh, reserved_wh, cancel_pending, binding_onchain: false, message}`. Reserved (in-flight) Wh are never cancelled: the unreserved remainder is removed now, `cancel_pending` is true while the reserved part settles, and the order is then finalized as cancelled. Idempotent. 409 `not_cancellable` for filled/expired orders. |
| `GET /trades?limit` | Your trades in every state: `status` (`matched`/`submitted`/`confirmed`/`failed`), `failure_reason`, `tx_hash`, `block_number`. |

Order fields worth showing: `status` (`open`, `partially_filled`, `filled`, `cancelled`, `expired`), `filled_wh`,
`reserved_wh`, `remaining_wh`, `cancel_pending`, `cancel_type` (`offchain` = "Cancelled", `onchain` = "Cancelled
on-chain"), `cancel_reason` (`user`, `unfunded:balance`, `unfunded:allowance`, `dust_remainder`, `on_chain_cancel`,
`nonce_floor`).

## Admin

| Endpoint | Notes |
|---|---|
| `GET /admin/trading/state` | Which roles have a leader, matching paused?, relayer halt reason, settlement counts by status, open orders, pairs on cooldown, listener cursor/head/lag. |
| `POST /admin/trading/pause`, `/resume` | Stop/restart matching and new submissions (in-flight transactions are still tracked). |

## Redis feed (forwarded by the `/ws` gateway, see "WebSocket (Phase 5)")

Same envelope as `price:{marketId}`: `{type, channel, seq, ts, data}` with a per-channel `seq`.
`orderbook:{marketId}` (`type: "orderbook"`, aggregated levels), `trades:{marketId}` (`type: "trade"`, confirmed
trades), `user:{address}` (`type: "order"` on every order change, `"fill"` on settlement state changes).
Events are published only after the database transaction commits.

# WebSocket (Phase 5)

One endpoint, `GET /ws` (upgrade to WebSocket), streams everything that changes: prices and their drivers,
the order book, the trade tape, a user's own orders and fills, and a meter owner's readings and mints.
REST remains the source of truth; the socket delivers **deltas** and REST delivers **snapshots**
(see [Staying consistent](#staying-consistent-snapshot--delta)).

> Testnet demo. Meter messages come from the labeled simulator and carry `"simulated": true`.

```bash
# anonymous, public channels (dev only: a missing Origin is accepted when API_ENV=development)
# NOTE: an unset API_ENV means production; local dev sets API_ENV=development explicitly.
npx wscat -c ws://localhost:8000/ws
> {"op":"subscribe","channels":["price:0x…marketId"],"id":"1"}
# in a browser-like context pass the Origin and, for user:/meter: channels, the session cookie
npx wscat -c ws://localhost:8000/ws -H "Origin: http://localhost:3000" -H "Cookie: wattswap_session=<jwt>"
```

## Handshake, Origin and authentication

| Rule | Behaviour |
|---|---|
| **Origin allow-list** | The `Origin` header must exactly match `API_CORS_ORIGINS` or `WS_EXTRA_ORIGINS` (case-insensitive, trailing `/` ignored). Anything else is refused **before the upgrade** with `HTTP 403`. Cookies alone do not stop cross-site WebSocket hijacking (a browser attaches them to any site's socket), so this check is mandatory. |
| **Missing Origin** | Non-browser clients (wscat, scripts) send none. It is accepted **only when `API_ENV=development`**. `API_ENV` defaults to `production`, so an unset value, `test` and `production` all refuse it with `403`. |
| **Authentication** | The `wattswap_session` cookie from `POST /auth/login`, read at the handshake. No token in the URL or in a first message, so credentials never reach logs. No cookie, or an invalid/revoked one, means an anonymous socket: public channels only. |
| **Authorization** | `user:{address}` only for the address in the JWT. `meter:{meterId}` only for the meter's owner. A failed subscribe is an `error` frame; the socket stays open and nothing from that channel is ever sent. |
| **Session end** | Logout closes that session's sockets on **every replica** with `4401 session_revoked` (a `ws:control` Redis message, backed by a denylist re-check every `WS_DENYLIST_POLL_SECONDS` in case that message is lost). JWT expiry closes the socket at `exp` with `4401 session_expired`. |

Production note: the browser sends the cookie to the API only if app and API are the same **site**; see
`docs/adr/009-realtime-gateway.md` (shared registrable domain).

## Channels

`{market}` and `{meterId}` are lowercase `0x` + 64 hex; `{address}` is lowercase `0x` + 40 hex (any case is accepted
in requests and normalised). Unknown markets are refused (`unknown_market`).

| Channel | Access | Message `type`s | Published by | Retained for resume |
|---|---|---|---|---|
| `price:{market}` | public | `price` | pricing engine, every tick (15 s) | 500 msgs / 1 h |
| `forecast:{market}` | public | `forecast` | pricing engine, when the 24 h outlook is recomputed (5–10 min) | 50 / 1 h |
| `orderbook:{market}` | public | `orderbook` | after every order/trade change (full depth, no addresses) | 5 000 / 10 min |
| `trades:{market}` | public | `trade` | when a settlement is confirmed | 5 000 / 10 min |
| `user:{address}` | that address | `order`, `fill` | every change to the user's orders / trades | 1 000 / 30 min |
| `meter:{meterId}` | the meter's owner | `reading`, `mint` | reading ingest; mint worker transitions | 1 000 / 30 min |
| `loadtest:{name}` | public, **dev only** (`WS_LOADTEST_ENABLED=true`, refused when `API_ENV=production`) | any | `make ws-load` | 1 000 / 1 min |

Retention windows are env-configurable (`WS_RETAIN_*`). A message is retained until it is older than the window **or**
the count is exceeded, whichever comes first.

## Frames

### Server → client data frame (the envelope)

```json
{"type":"orderbook","channel":"orderbook:0x7f…","epoch":"9c1f03ab77d2e418","seq":412,"ts":1792401331,"data":{ … }}
```

| Field | Meaning |
|---|---|
| `type` | Message kind (see below). |
| `channel` | The channel it belongs to. |
| `epoch` | Random id of the channel's stream, created when its state is first created and **recreated if that state is lost** (a Redis reset). A `seq` is only comparable within one epoch. Remember it with `seq`. |
| `seq` | Per-channel counter within the epoch, **+1 per message, no gaps**. Allocated atomically with the publish, so order on the wire equals `seq` order. |
| `ts` | Unix seconds at publish. |
| `data` | Type-specific body. Amounts and prices are decimal **strings** (Wh; mUSDC base units per kWh); never JS floats. |

### Data bodies

`orderbook` — full depth, state-replacing (a client replaces its book with this):
```json
{"market_id":"0x7f…","bids":[{"price":"160000","amount_wh":"2000","orders":1}],"asks":[{"price":"140000","amount_wh":"1000","orders":2}],"ts":1792401331}
```

`trade` — a confirmed settlement (public tape):
```json
{"id":"7d1c…","market_id":"0x7f…","buyer":"0x9965…","seller":"0x3C44…","fill_wh":"500","exec_price":"235179","cost":"117589","fee":"1175","status":"confirmed","failure_reason":null,"tx_hash":"0x6088…","block_number":39743,"executed_at":"2026-10-19T09:20:30Z","created_at":"2026-10-19T09:20:28Z","external":false}
```

`order` — your order changed (same body as `GET /orders/{hash}`, keyed by `order_hash`); `fill` — one of your trades changed state (same body as `trade`, keyed by `id`; `status` goes `matched → submitted → confirmed | failed`).

`price` — body of `GET /markets/{id}/price`, including the **price drivers** the Market Insights page renders (numbers below are illustrative):
```json
{"marketId":"0x7f…","price":235179,"rawPrice":235179,"emaPrice":"235179.000","supplyW":2829,"demandW":4500,
 "bandBps":500,"breaker":"NORMAL","breakerReason":null,"weatherStale":false,"halted":false,"volBps":"1.2000",
 "ts":1792401331,"marketTime":1792401331,"clockMode":"real","accelerated":false,"quote":{…signed PriceQuote…},
 "events":[],
 "drivers":{"version":2,"local_solar_hour":"14.250","profile_multiplier":"1.1000",
   "supply":{"generation_kw":"2.828800","inventory_kw":"0","supply_kw":"…","effective_kw":"…","floor_applied":false,…},
   "demand":{"load_kw":"4.950000","bids_kw":"…","velocity_kw":"…","demand_kw":"…",…},
   "price":{"ratio":"1.7500","epsilon":"0.8","p_base":150000,"raw":235179,…},
   "smoothing":{"ema":"…","alpha":"0.3","applied":235179,"limiter":null,…},
   "weather":[{"source":"live","age_s":0,…}],"breaker":{…},"band":{…},"attribution":{…},"terms":{…}}}
```
`drivers.*` is exactly what the engine used for that tick (`docs/pricing-model.md` explains every term), so the UI
explains a price move from the same message that moved it.

`forecast` — hourly outlook for the next 24 h: `{"marketId","generatedAt","marketTime","note","hours":[{"time","ghi_wm2","generation_kw","supply_kw","demand_kw","raw_price","clamps"}]}` (body of `GET /markets/{id}/forecast`).

`reading` (meter owner) — an accepted reading: the `ReadingOut` body plus `"simulated": true`.
`mint` (meter owner) — `{"status":"submitted|confirmed|failed","tx_hash","block_number","attempts","failure_reason","confirmed_at","reading_id","meter_id","simulated"}`.

### Server → client control frames (no `seq`)

| `type` | Body | When |
|---|---|---|
| `subscribed` | `ref`, `channels:[{channel, epoch, seq, replayed}]` | After a subscribe. `epoch`/`seq` = the channel's epoch and head when the subscription went live; the live stream continues at `seq + 1`. `replayed` = messages replayed just before this frame (resume). |
| `unsubscribed` | `ref`, `channels:[…]` | After an unsubscribe. |
| `resync` | `channel`, `reason`, `epoch`, `seq` | The client cannot be brought up to date from the log: re-fetch the REST snapshot. `reason`: `epoch_changed` (the channel's state was lost and recreated; also sent on a **live** socket right before the first message of the new epoch, whose seqs restart), `too_old` (outside the retention window), `ahead_of_head` (client seq > head of the same epoch), `too_many` (more than `WS_RESUME_MAX_MESSAGES` missed). `epoch`/`seq` are the channel's current epoch and head. |
| `error` | `code`, `message`, `ref?`, `channel?` | A request failed; the socket stays open. Codes: `bad_channel`, `unknown_market`, `unauthorized`, `forbidden`, `too_many_subscriptions`, `rate_limited`, `bad_message`. |
| `ping` | `ts` | Every `WS_PING_SECONDS` (20 s). Reply `{"op":"pong"}` (any inbound frame also counts as liveness). |
| `pong` | `ref?` | Reply to a client `ping`. |

### Client → server ops

All frames are JSON text, ≤ `WS_MAX_MESSAGE_BYTES` (4 KiB), strictly validated (unknown fields are rejected).

```json
{"op":"subscribe","channels":["orderbook:0x7f…","user:0x9965…"],"id":"1","resume":{"orderbook:0x7f…":{"epoch":"9c1f03ab77d2e418","seq":412}}}
{"op":"unsubscribe","channels":["orderbook:0x7f…"],"id":"2"}
{"op":"ping","id":"3"}
{"op":"pong"}
```
`resume` maps a channel to the **epoch and last seq the client applied** (both are required). If the epoch still matches and the log
covers the gap, the server replays everything after `seq`, in order, *before* the `subscribed` frame; otherwise it sends `resync`. Subscribing to an already-joined channel is idempotent.

Example session (`→` client, `←` server):
```
→ {"op":"subscribe","channels":["orderbook:0x7f…"],"id":"1"}
← {"type":"subscribed","ref":"1","channels":[{"channel":"orderbook:0x7f…","epoch":"9c1f03ab77d2e418","seq":411,"replayed":0}]}
← {"type":"orderbook","channel":"orderbook:0x7f…","epoch":"9c1f03ab77d2e418","seq":412,"ts":1792401331,"data":{…}}
← {"type":"ping","ts":1792401351}
→ {"op":"pong"}
```

## Staying consistent: snapshot + delta

REST snapshots tell you *what is true now and which `(epoch, seq)` that corresponds to*. The socket then sends everything after that seq.

| Snapshot | Channel | How `epoch` and `seq` are returned |
|---|---|---|
| `GET /markets/{id}/book` | `orderbook:{market}` | body fields `epoch`, `seq` |
| `GET /markets/{id}/trades` | `trades:{market}` | response headers `X-Channel-Epoch`, `X-Channel-Seq` |
| `GET /orders`, `GET /trades` (yours) | `user:{you}` | response headers `X-Channel-Epoch`, `X-Channel-Seq` |
| `GET /markets/{id}/price` | `price:{market}` | body fields `epoch`, `seq` |

**`X-Channel-Seq` and `X-Channel-Epoch`** are listed in `Access-Control-Expose-Headers`, so a script on an allowed web origin can
read them (`fetch(url,{credentials:"include"}).then(r => r.headers.get("x-channel-epoch"))`). Without that a browser hides
them from cross-origin pages.

**The server reads the channel's head `(epoch, seq)` *before* it reads the data.** Therefore every change after the snapshot has a
`seq` greater than the one returned; a change committed in between may appear in both the snapshot and a delta.
That is safe because **deltas are idempotent by construction** — `orderbook` replaces the whole book, `order`/`fill`
upsert by `order_hash`/`id`, `trade` is keyed by `id`, `price`/`forecast` replace. This is a rule for every new message type.

Client algorithm (per channel; reference implementation: `services/api/dev/ws_sync.py`, tested in
`tests/realtime/test_sync_protocol.py`):

1. Connect, `subscribe`. Receive `subscribed{epoch: E, seq: S0}`. **Buffer** all deltas.
2. `GET` the REST snapshot → `(data, E', Ssnap)`.
3. Install the snapshot; apply buffered deltas **of epoch E'** with `seq > Ssnap` in order, dropping `seq <= Ssnap` and any other epoch. The first applied must be `Ssnap + 1`; if the buffer starts later, or its newest message is from a different epoch than the snapshot, the snapshot was too old — fetch again.
4. Live: **epoch differs → the channel was reset: stop applying, buffer, go to step 2** (never compare seqs across epochs: after a reset the new messages start at seq 1 and would look like duplicates). Same epoch: `seq == last+1` → apply; `seq <= last` → ignore (duplicate); `seq > last+1` → **gap**: stop applying, buffer, go to step 2.
5. On `resync` for the channel: go to step 2.
6. On reconnect: `subscribe` again with `resume:{channel:{epoch, seq: last}}`; if the server answers `resync`, go to step 2, otherwise the replay continues seamlessly (no snapshot needed).

```mermaid
sequenceDiagram
    participant C as Client
    participant G as Gateway (any replica)
    participant R as Redis (stream + pub/sub)
    participant A as REST API
    C->>G: subscribe orderbook (resume: none)
    G->>R: SUBSCRIBE channel, read head seq
    G-->>C: subscribed {seq: 411}
    R-->>G: seq 412, 413 (live)
    G-->>C: 412, 413  (client buffers: snapshot not loaded yet)
    C->>A: GET /book
    A->>R: read head seq (412)  -- BEFORE reading the data
    A-->>C: snapshot {seq: 412, bids, asks}
    Note over C: drop buffered 412 (<= snapshot), apply 413
    R-->>G: seq 414
    G-->>C: 414 (live, last+1: apply)
    R--xG: seq 415 lost in transit
    R-->>G: seq 416
    G-->>C: 416
    Note over C: 416 != 414+1 => GAP: buffer, stop applying
    C->>A: GET /book
    A-->>C: snapshot {seq: 416}
    Note over C: state = snapshot; deltas > 416 apply from here
    C--xG: connection drops
    C->>G: subscribe (resume: {orderbook: {epoch: E, seq: 418}})
    G->>R: XRANGE seq > 418 up to head (421)
    G-->>C: 419, 420, 421 (replayed in order)
    G-->>C: subscribed {seq: 421, replayed: 3}
    Note over C: no snapshot needed
```

**Epoch change (Redis reset).** If the channel's state is lost, its next publish creates a new epoch and restarts `seq` at 1.
A connected client then receives `resync{reason:"epoch_changed"}` followed by messages whose `epoch` differs from the one it holds;
a reconnecting client's `resume` is answered with the same `resync`. Either way the client re-snapshots; it never silently
ignores the new messages. Covered by `test_redis_flush_mid_session_is_detected_and_the_client_resyncs`.

Resume safety: the server subscribes to live delivery first, buffers live messages, replays `last+1 .. head` from the
retained log, then flushes buffered live messages with `seq > head` — so there is neither a hole nor a duplicate.

## Heartbeat, backpressure and limits

* **Heartbeat** is application-level JSON (browsers cannot send or observe WebSocket control-frame pings). The server pings every `WS_PING_SECONDS`; if it receives no frame at all for 2 intervals it closes with `4408`.
* **Backpressure**: each socket has a bounded send queue (`WS_SEND_QUEUE`, 256). If a client does not read fast enough the queue overflows — or one send stalls longer than `WS_SEND_TIMEOUT_SECONDS` — and *that socket only* is closed with `4409`; other clients are unaffected. The client reconnects and resumes.
* **Shutdown**: graceful close `1001` so clients reconnect (and resume) against another replica.

| Limit | Default (env) | On breach |
|---|---|---|
| Subscriptions per connection | 20 (`WS_MAX_SUBSCRIPTIONS`) | `error: too_many_subscriptions`, socket stays open |
| Inbound frame size | 4096 B (`WS_MAX_MESSAGE_BYTES`) | close `4413` |
| Inbound rate | 20 msgs / 10 s sustained, burst 40 (`WS_RATE_PER_WINDOW`, `WS_RATE_WINDOW_SECONDS`, `WS_RATE_BURST`) | `error: rate_limited`; after 3 breaches (`WS_RATE_STRIKES`) close `4429` |
| Connections per IP / per address | 20 / 10 (`WS_MAX_CONN_PER_IP`, `WS_MAX_CONN_PER_ADDRESS`) | upgrade succeeds, then close `4429 too_many_connections` (counted in Redis, so the limit holds across replicas; slots expire if a replica dies) |
| Resume replay | 1 000 msgs (`WS_RESUME_MAX_MESSAGES`) | `resync: too_many` |

### Close codes

| Code | Name | Meaning | Client should |
|---|---|---|---|
| HTTP 403 | (before upgrade) | Origin not allowed / missing Origin outside development | Fix configuration; do not retry |
| 1000 | normal | Client closed | – |
| 1001 | going away | Server shutting down | Reconnect with backoff, resume |
| 4400 | bad message | Malformed JSON, binary frame, unknown op or field, invalid shape | Fix the client; do not retry blindly |
| 4401 | session ended | JWT expired (`session_expired`) or logged out (`session_revoked`) | Sign in again, then reconnect |
| 4408 | heartbeat timeout | Nothing received for 2 ping intervals | Reconnect, resume |
| 4409 | slow consumer | Send queue overflow / stalled send | Reconnect, resume (and read faster) |
| 4413 | message too large | Inbound frame over the limit | Fix the client |
| 4429 | rate limited | Sustained message-rate breach or too many connections | Back off, then reconnect |

Suggested reconnect policy: exponential backoff 0.5 s → 30 s with jitter; treat `4400`/`4413` as bugs, `4401` as "sign in".

## Several API replicas

Any number of API processes can serve `/ws`. Every message is published to Redis by whichever process produced it
(matcher, listener, pricing engine, REST handlers) using one atomic script that allocates the `seq`, appends to the
channel's stream and broadcasts. The envelope is serialized once there; each gateway parses it once per message to read
`(epoch, seq)` and hands the **same string object** to every local client (`test_one_parse_and_one_shared_string_for_all_clients`). Each gateway keeps **one** Redis subscription per channel it has local clients
for. A client may connect to any replica, and may resume on a different one than it left.
Proof: `make ws-fanout-demo` (two replicas in docker compose; a trade driven through replica 2 reaches clients on both
with identical `(channel, seq)` sequences) and `tests/realtime/test_replicas.py`.

If a gateway loses its Redis pub/sub connection it reconnects, re-subscribes, and replays every local socket from its
own last-sent seq (or sends `resync`).

Redis keys are hash-tagged per channel (`{chan:<channel>}:seq|stream|latest|epoch`) so the publish script stays valid on Redis Cluster.

## Metrics

`wattswap_ws_connections`, `wattswap_ws_handshakes_total{result}`, `wattswap_ws_closes_total{code}`, `wattswap_ws_subscriptions`, `wattswap_ws_messages_out_total{kind}`, `wattswap_ws_send_queue_depth`, `wattswap_ws_resume_total{outcome}`.

## Load test

`WS_LOADTEST_ENABLED=true WS_MAX_CONN_PER_IP=2000 docker compose up -d api` then `make ws-load ARGS="--clients 500 --messages 200 --rate 10"` (dev stack only; it publishes to a dedicated `loadtest:run-<timestamp>` channel, never to a real market). Results and method: `docs/phases/phase-5.md`.
