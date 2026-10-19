# ADR-009: Realtime gateway (`/ws`), retained streams and cookie-authenticated sockets

Status: accepted (Phase 5) · Related: ADR-006 (SIWE + JWT), ADR-008 (trading architecture)

## Context

The browser needs prices, the book, the tape and its own orders in real time, over several API
replicas, without ever being silently wrong. Phases 3 and 4 published `{type, channel, seq, ts, data}`
envelopes over Redis pub/sub with a per-channel counter, but pub/sub is fire-and-forget: nothing
was retained for a reconnecting client, and the `INCR` + `PUBLISH` pair was two round trips.

## Decisions

1. **One atomic publish script per message.** A Lua script does `INCR seq`, `XADD` (stream entry id
   = `<seq>-0`), retention trimming and `PUBLISH`. Two concurrent publishers can no longer deliver
   seq 6 before seq 5 (the Phase 4 publisher could; `tests/realtime/test_publisher.py` reproduces the
   old pattern reordering and proves the script does not). Delivery order on a channel equals seq order.
2. **Redis Streams are the retained log**, so a client reconnecting inside the window resumes from its
   last seq instead of re-snapshotting. Retention is by count (exact `MAXLEN`) and by age, per channel
   class (public 5 000 / 10 min, user 1 000 / 30 min, price 500 / 1 h; env-configurable). Outside the
   window, or if Redis was reset, the server answers `resync` and the client falls back to REST.
   *Limitation:* there is no stream epoch, so if Redis is flushed and a channel is re-published past a
   client's old seq, that client can be replayed the wrong history. Mitigation is operational (Redis
   persistence); an epoch field is the next step if Redis is ever treated as ephemeral.
3. **Keys are hash-tagged per channel** (`{chan:<channel>}:seq|stream|latest`) so the script touches
   one slot and stays valid on Redis Cluster. Pub/sub itself is cluster-wide.
4. **Snapshot consistency = "read the seq first".** REST snapshots return the channel's head seq read
   *before* the data (`seq` in the `/book` body; `X-Channel-Seq` header on list endpoints, exposed
   via CORS so the web origin can read it). Anything after the snapshot has a larger seq; an event that
   lands in both is harmless because deltas are idempotent (state-replacing or keyed upserts). That is a
   rule for every new message type. The book publish is serialised per market (compute + publish
   under a lock with a dirty flag), because computing then publishing could attach an older book to a
   newer seq.
5. **Application-level JSON heartbeat**, not WebSocket control-frame pings: browsers can neither send
   nor observe those, and JSON works through every proxy and with `wscat`.
6. **Backpressure = drop the slow socket, not the fast ones.** Each socket has a bounded queue; overflow
   or a send stalled past a timeout closes that socket with `4409`, and it resumes on reconnect. A
   per-send timer was removed from the hot path (a stalled send is detected on the next enqueue and
   by the heartbeat), which cut p95 delivery latency under load by roughly half.
7. **Cookie-only authentication, Origin allow-list before the upgrade.** The httpOnly session cookie is
   the only credential (no token in the URL or first message, so it never reaches access logs or
   history). A browser attaches cookies to a WebSocket opened by *any* site, so the `Origin` header
   is checked against an exact allow-list and a mismatch is refused with HTTP 403 before the socket
   exists. A missing Origin (non-browser client) is accepted only when `API_ENV` was **explicitly**
   `development`.
8. **Session end is enforced on open sockets.** Each socket schedules its own close at the JWT `exp`
   (`4401`); logout publishes the `jti` on a Redis control channel so every replica closes that session's
   sockets, with a denylist re-check on the heartbeat as a backstop for a lost control message.
9. **Fan-out**: one Redis pub/sub connection per process, channels subscribed on first local subscriber
   and released on the last; the envelope string is forwarded unchanged (no per-client re-encode). If
   the connection drops, the hub reconnects and replays each socket from its own last-sent seq.

## Deployment requirement: app and API must be the same site

Cookie authentication only works if the browser sends the session cookie to the API origin for both
`fetch` and the WebSocket handshake. The cookie is `HttpOnly; Secure; SameSite=Lax`. `SameSite=Lax`
cookies are sent on requests between origins of the same **site**, i.e. the same *registrable domain*
(eTLD+1), not on cross-site subrequests. Therefore in production:

* serve the web app and the API from subdomains of one registrable domain, e.g. `app.example.com` and
  `api.example.com`; `API_CORS_ORIGINS` / `WS_EXTRA_ORIGINS` list the exact web origin(s);
* the default is a **host-only** cookie on the API host, which is sufficient (the browser sends it to
  `api.example.com` for requests initiated from `app.example.com`). Set `SESSION_COOKIE_DOMAIN=.example.com`
  only if another subdomain must also receive it. Do not widen it to a parent shared with untrusted sites;
* `SameSite` stays `Lax`, never `None`: `None` would send the cookie on genuinely cross-site requests and
  trade away CSRF protection for the REST API. If the web app were hosted on a different registrable
  domain (for example `*.vercel.app` talking to `api.example.com`) the cookie would not be sent, and the
  fix is to move to a shared domain or to proxy the API through the web origin, not to loosen the cookie;
* the domain must not be a public suffix (the browser would refuse a `Domain` on `vercel.app`, `github.io`, …);
* `Secure` requires HTTPS (`wss://`). Browsers treat `http://localhost` as secure, which is why local dev works.

This is on the Phase 8 checklist (`docs/phases/phase-8.md`).

## Consequences

* Resume works across replicas and across deploys inside the retention window; beyond it clients
  re-snapshot, which they must support anyway.
* A burst of book changes publishes at most one book per recompute under the lock; the last message
  always equals the database.
* One gateway process is CPU-bound by per-frame Python overhead (measured in `docs/phases/phase-5.md`);
  capacity scales with replicas because Redis is the only shared state.
* Streams cost Redis memory (bounded by the count caps, independent of traffic). `user:` streams hold
  private order data inside the Redis trust boundary (threat model W-9).
