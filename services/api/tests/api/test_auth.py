import time
from datetime import UTC, datetime, timedelta

import jwt
import pytest
from eth_account import Account
from eth_account.messages import encode_defunct
from meter_sim.provision import COOKIE_NAME, build_siwe_message

from app.auth.tokens import ALGORITHM
from tests.conftest import KEY_ALICE, KEY_BOB, SIWE_DOMAIN, Harness, SettingsFactory
from tests.helpers import login


async def _signed_message(
    h: Harness,
    key: str,
    *,
    domain: str = SIWE_DOMAIN,
    chain_id: int = 31337,
    issued_at: datetime | None = None,
    expires_in: timedelta = timedelta(minutes=5),
    nonce: str | None = None,
) -> tuple[str, str]:
    account = Account.from_key(key)
    if nonce is None:
        nonce = (await h.client.get("/api/v1/auth/nonce")).json()["nonce"]
    message = build_siwe_message(
        domain=domain,
        address=account.address,
        uri=f"http://{domain}",
        chain_id=chain_id,
        nonce=nonce,
        issued_at=issued_at or datetime.now(UTC),
        expires_in=expires_in,
    )
    sig = account.sign_message(encode_defunct(text=message)).signature.hex()
    return message, "0x" + sig


async def _login_raw(h: Harness, message: str, signature: str) -> int:
    r = await h.client.post("/api/v1/auth/login", json={"message": message, "signature": signature})
    return r.status_code


async def test_login_sets_httponly_secure_lax_cookie_with_12h_lifetime(harness: Harness) -> None:
    message, sig = await _signed_message(harness, KEY_ALICE)
    r = await harness.client.post("/api/v1/auth/login", json={"message": message, "signature": sig})
    assert r.status_code == 200
    assert r.json()["address"] == Account.from_key(KEY_ALICE).address
    assert r.json()["role"] == "user"
    cookie = r.headers["set-cookie"].lower()
    assert f"{COOKIE_NAME}=" in cookie
    for flag in ("httponly", "secure", "samesite=lax", "max-age=4"):  # 43 200 s (+/- a second)
        assert flag in cookie
    token = r.cookies[COOKIE_NAME]
    claims = jwt.decode(
        token,
        harness.settings.jwt_secret.get_secret_value(),
        algorithms=[ALGORITHM],
        issuer="wattswap-api",
    )
    assert 43_190 <= claims["exp"] - claims["iat"] <= 43_200
    me = await harness.client.get("/api/v1/auth/me")
    assert me.status_code == 200 and me.json()["address"] == r.json()["address"]


async def test_me_requires_authentication(harness: Harness) -> None:
    r = await harness.client.get("/api/v1/auth/me")
    assert r.status_code == 401
    body = r.json()["error"]
    assert body["code"] == "unauthorized" and body["request_id"] == r.headers["x-request-id"]


async def test_nonce_is_single_use(harness: Harness) -> None:
    message, sig = await _signed_message(harness, KEY_ALICE)
    assert await _login_raw(harness, message, sig) == 200
    assert await _login_raw(harness, message, sig) == 401  # replay with the same nonce


async def test_unknown_nonce_rejected(harness: Harness) -> None:
    message, sig = await _signed_message(harness, KEY_ALICE, nonce="forgednonce12345")
    assert await _login_raw(harness, message, sig) == 401


async def test_bad_signature_burns_the_nonce(harness: Harness) -> None:
    message, _ = await _signed_message(harness, KEY_ALICE)
    wrong = Account.from_key(KEY_BOB).sign_message(encode_defunct(text=message)).signature.hex()
    assert await _login_raw(harness, message, "0x" + wrong) == 401
    good = Account.from_key(KEY_ALICE).sign_message(encode_defunct(text=message)).signature.hex()
    assert await _login_raw(harness, message, "0x" + good) == 401  # nonce already consumed


@pytest.mark.parametrize(
    "kwargs",
    [
        {"domain": "evil.example"},
        {"chain_id": 1},
        {"expires_in": timedelta(seconds=-5)},  # already expired
        {"issued_at": datetime.now(UTC) - timedelta(hours=2), "expires_in": timedelta(hours=3)},
        {"issued_at": datetime.now(UTC) + timedelta(hours=1), "expires_in": timedelta(hours=3)},
    ],
    ids=["wrong-domain", "wrong-chain", "expired", "stale-issued-at", "future-issued-at"],
)
async def test_invalid_siwe_messages_rejected(harness: Harness, kwargs: dict[str, object]) -> None:
    message, sig = await _signed_message(harness, KEY_ALICE, **kwargs)  # type: ignore[arg-type]
    assert await _login_raw(harness, message, sig) == 401


async def test_malformed_message_and_signature_rejected(harness: Harness) -> None:
    assert await _login_raw(harness, "hello", "0x00") == 401
    message, _ = await _signed_message(harness, KEY_ALICE)
    assert await _login_raw(harness, message, "0xdeadbeef") == 401
    assert await _login_raw(harness, message, "not-hex") == 401


async def test_logout_revokes_the_token_server_side(harness: Harness) -> None:
    s = await login(harness, KEY_ALICE)
    assert (await harness.client.get("/api/v1/auth/me", headers=s.headers)).status_code == 200
    out = await harness.client.post("/api/v1/auth/logout", headers=s.headers)
    assert out.status_code == 204
    cleared = out.headers["set-cookie"].lower()
    assert f"{COOKIE_NAME}=" in cleared and "max-age=0" in cleared
    # The very same (still unexpired) cookie must now be refused: the jti is on the denylist.
    again = await harness.client.get("/api/v1/auth/me", headers=s.headers)
    assert again.status_code == 401
    assert "logged out" in again.json()["error"]["message"]
    # A new login works and gets a new token.
    s2 = await login(harness, KEY_ALICE)
    assert s2.cookie != s.cookie
    assert (await harness.client.get("/api/v1/auth/me", headers=s2.headers)).status_code == 200


async def test_logout_requires_a_session(harness: Harness) -> None:
    assert (await harness.client.post("/api/v1/auth/logout")).status_code == 401


async def test_tampered_expired_and_wrong_secret_tokens_rejected(harness: Harness) -> None:
    s = await login(harness, KEY_ALICE)
    secret = harness.settings.jwt_secret.get_secret_value()
    good = jwt.decode(s.cookie, secret, algorithms=[ALGORITHM], issuer="wattswap-api")

    def cookie(token: str) -> dict[str, str]:
        return {"cookie": f"{COOKIE_NAME}={token}"}

    forged = jwt.encode(good, "some-other-secret-some-other-secret-xx", algorithm=ALGORITHM)
    expired = jwt.encode({**good, "exp": int(time.time()) - 10}, secret, algorithm=ALGORITHM)
    hs512 = jwt.encode(good, secret, algorithm="HS512")
    none_alg = jwt.encode(good, None, algorithm="none")
    flipped = s.cookie[:-2] + ("AA" if not s.cookie.endswith("AA") else "BB")
    for bad in (forged, expired, hs512, none_alg, flipped, "garbage"):
        r = await harness.client.get("/api/v1/auth/me", headers=cookie(bad))
        assert r.status_code == 401, bad


async def test_admin_role_comes_from_allow_list(make_settings: SettingsFactory) -> None:
    alice = Account.from_key(KEY_ALICE).address
    async with Harness(make_settings(admin_addresses=f"{alice.lower()}, 0x123")) as h:
        a = await login(h, KEY_ALICE)
        b = await login(h, KEY_BOB)
        assert (await h.client.get("/api/v1/auth/me", headers=a.headers)).json()["role"] == "admin"
        assert (await h.client.get("/api/v1/auth/me", headers=b.headers)).json()["role"] == "user"


async def test_auth_endpoints_are_rate_limited(make_settings: SettingsFactory) -> None:
    async with Harness(make_settings(rate_limit_auth=3)) as h:
        codes = [(await h.client.get("/api/v1/auth/nonce")).status_code for _ in range(5)]
        assert codes == [200, 200, 200, 429, 429]
        limited = await h.client.get("/api/v1/auth/nonce")
        assert limited.json()["error"]["code"] == "rate_limited"
        assert int(limited.headers["retry-after"]) >= 1


async def test_error_envelope_for_validation_and_not_found(harness: Harness) -> None:
    bad = await harness.client.post("/api/v1/auth/login", json={"message": 1})
    assert bad.status_code == 422
    err = bad.json()["error"]
    assert err["code"] == "validation_error" and err["details"]["errors"]
    missing = await harness.client.get("/api/v1/nope")
    assert missing.status_code == 404 and missing.json()["error"]["code"] == "not_found"
    assert missing.json()["error"]["request_id"]


async def test_cors_only_allows_configured_origins(harness: Harness) -> None:
    ok = await harness.client.options(
        "/api/v1/auth/nonce",
        headers={"origin": "http://localhost:3000", "access-control-request-method": "GET"},
    )
    assert ok.headers.get("access-control-allow-origin") == "http://localhost:3000"
    assert ok.headers.get("access-control-allow-credentials") == "true"
    evil = await harness.client.options(
        "/api/v1/auth/nonce",
        headers={"origin": "https://evil.example", "access-control-request-method": "GET"},
    )
    assert "access-control-allow-origin" not in evil.headers
