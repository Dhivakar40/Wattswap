# Phase 8 — Sepolia deployment, env configs, final docs (checklist)

Status: **NOT STARTED.** This file only accumulates requirements discovered in earlier phases so that none is
forgotten; the full plan is written when the phase begins (CLAUDE.md §6).

## Carried-over requirements

- [ ] **Same-site hosting for cookie auth (from Phase 5, ADR-009).** Serve the web app and the API from subdomains of
      one registrable domain (e.g. `app.<domain>` and `api.<domain>`), over HTTPS/`wss://`. The session cookie is
      `HttpOnly; Secure; SameSite=Lax` and host-only on the API host; `SameSite` must not be loosened to `None`.
      Verify before launch:
  - [ ] `API_CORS_ORIGINS` (and `WS_EXTRA_ORIGINS` if needed) list the exact web origin(s) with `https://`.
  - [ ] `SESSION_COOKIE_DOMAIN` is empty (host-only) unless another subdomain must receive the cookie; never a public suffix.
  - [ ] `SIWE_DOMAIN` matches the web app's host.
  - [ ] `API_ENV=production` (a missing `Origin` on `/ws` is then refused) and `JWT_SECRET` is a random 32+ char value.
  - [ ] Smoke test from the deployed web origin: login, `fetch` a list endpoint with `credentials: "include"` and read
        `X-Channel-Seq`, open `wss://api.<domain>/ws`, subscribe to a `user:` channel, log out and see `4401`.
  - [ ] If a different registrable domain is unavoidable, proxy the API through the web origin instead of relaxing the cookie.
- [ ] Redis persistence (AOF) or an accepted loss of the retained streams on restart (clients re-snapshot); decide
      whether to add a stream epoch (ADR-009, limitation).
- [ ] Re-run `make ws-load` against the deployed topology (replica count, real network) and record the p95.
- [ ] Reverse proxy / load balancer: WebSocket upgrade enabled, idle timeout > `WS_PING_SECONDS * 2`, forwarded client IP
      configured (the per-IP connection cap uses the socket peer address today).
