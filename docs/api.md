# API reference (Phases 2–4)

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

## Redis feed (consumed by the Phase 5 gateway)

Same envelope as `price:{marketId}`: `{type, channel, seq, ts, data}` with a per-channel `seq`.
`orderbook:{marketId}` (`type: "orderbook"`, aggregated levels), `trades:{marketId}` (`type: "trade"`, confirmed
trades), `user:{address}` (`type: "order"` on every order change, `"fill"` on settlement state changes).
Events are published only after the database transaction commits.
