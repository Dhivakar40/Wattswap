# ADR-006: SIWE sessions, JWT cookie, and role-key separation

Status: accepted · Date: 2026-10-18

## Decisions

**Sign-In with Ethereum.** `GET /auth/nonce` issues a random single-use nonce (Redis, 5 min). `POST /auth/login`
parses the EIP-4361 message (the `siwe` library), requires the configured domain and chain id, an `issued_at`
within 10 minutes and an `expiration_time`, **consumes the nonce atomically before verifying** (`GETDEL`, so even a
failed attempt burns it), then verifies the signature for EOAs and EIP-1271 contract wallets (the latter via the RPC).

**Session.** A JWT (HS256, algorithm pinned on decode, `iss`/`sub`/`jti`/`iat`/`exp` required) lives **12 hours** in
a cookie that is `HttpOnly`, `Secure` and `SameSite=Lax`. Browsers accept `Secure` cookies on `http://localhost`;
non-browser dev clients attach the cookie header explicitly. The secret comes from `JWT_SECRET` and must be a
random ≥ 32 characters in production (the process refuses to start otherwise).

**Revocation.** `POST /auth/logout` stores the token's `jti` in a Redis denylist for its remaining lifetime and
clears the cookie. Every authenticated request checks the denylist, so a stolen-then-logged-out cookie is dead
immediately even though the JWT itself has not expired. There are no refresh tokens: after 12 h the user signs in again.

**CSRF.** `SameSite=Lax` keeps the cookie off cross-site `POST`/`PATCH`; the API only accepts JSON bodies; CORS is an
allow-list of configured origins. No state-changing endpoint is reachable with a plain `GET`.

**Admin.** The `ADMIN_ADDRESSES` environment allow-list is authoritative and re-evaluated on every login.

**Role-key separation.** The API holds distinct dev keys for the oracle (`MINTER_ROLE`), registrar
(`REGISTRAR_ROLE`), relayer and pricer. If any two configured keys resolve to the same address the process refuses
to start (checked in `Settings` validation and again in `create_app`). `/ready` additionally verifies the oracle
and registrar really hold their on-chain roles and that `EnergyToken` holds `RECORDER_ROLE`. In production these keys
would live in a KMS/HSM, not environment variables.

**Device keys never reach the API.** The user supplies only the device *address* at registration; the device (or the
simulator) holds the key and signs readings.

## Consequences

- A compromised oracle key cannot register meters, and a compromised registrar key cannot mint.
- Logout is real, not cosmetic, at the cost of one Redis lookup per request.
- Rotating `JWT_SECRET` invalidates all sessions.
