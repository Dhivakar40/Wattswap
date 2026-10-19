"""Handshake policy and channel authorization: Origin, user channels, session end."""

import asyncio
import time
from collections.abc import AsyncIterator

import httpx
import pytest
from websockets.exceptions import InvalidStatus

from app.auth.tokens import issue_token
from app.core.config import Settings
from app.realtime.gateway import origin_allowed
from tests.conftest import KEY_ALICE, KEY_BOB, Harness, SettingsFactory
from tests.helpers import login
from tests.realtime.kit import MID, WEB_ORIGIN, Live, pub, seed_market

PRICE = f"price:{MID}"


@pytest.fixture
async def live(make_settings: SettingsFactory) -> AsyncIterator[Live]:
    async with Harness(make_settings()) as h:
        await seed_market(h)
        async with Live(h) as lv:
            yield lv


# ------------------------------------------------------------------------------------ Origin
def test_origin_allowed_is_exact_match() -> None:
    allowed = frozenset({"http://localhost:3000"})
    assert origin_allowed("http://localhost:3000", allowed, allow_missing=False)
    assert origin_allowed("HTTP://LOCALHOST:3000/", allowed, allow_missing=False)
    for bad in [
        "http://localhost:3001",
        "http://localhost:3000.evil.example",
        "https://localhost:3000",
        "null",
        "",
    ]:
        assert not origin_allowed(bad, allowed, allow_missing=True), bad
    assert not origin_allowed(None, allowed, allow_missing=False)
    assert origin_allowed(None, allowed, allow_missing=True)


def test_missing_origin_only_when_api_env_is_explicitly_development(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("API_ENV", raising=False)
    assert not Settings(_env_file=None).ws_allow_missing_origin  # unset => refuse
    assert Settings(_env_file=None, api_env="development").ws_allow_missing_origin
    assert not Settings(_env_file=None, api_env="test").ws_allow_missing_origin
    secret = "a-real-production-secret-0123456789abcdef"
    prod = Settings(_env_file=None, api_env="production", jwt_secret=secret)
    assert not prod.ws_allow_missing_origin
    monkeypatch.setenv("API_ENV", "development")
    assert Settings(_env_file=None).ws_allow_missing_origin  # from the environment counts
    monkeypatch.setenv("API_ENV", "test")
    assert not Settings(_env_file=None).ws_allow_missing_origin


async def test_listed_origin_connects_foreign_origin_gets_403(live: Live) -> None:
    async with live.connect(origin=WEB_ORIGIN) as c:
        await c.send(op="ping")
        await c.recv_type("pong")
    for evil in ["http://evil.example", "http://localhost:3000.evil.example", "null"]:
        with pytest.raises(InvalidStatus) as exc:
            async with live.connect(origin=evil):
                pass
        assert exc.value.response.status_code == 403


async def test_missing_origin_refused_unless_development(make_settings: SettingsFactory) -> None:
    async with Harness(make_settings(api_env="test")) as h, Live(h) as lv:
        with pytest.raises(InvalidStatus) as exc:
            async with lv.connect(origin=None):
                pass
        assert exc.value.response.status_code == 403
    async with Harness(make_settings(api_env="development")) as h, Live(h) as lv:
        async with lv.connect(origin=None) as c:
            await c.send(op="ping")
            await c.recv_type("pong")


async def test_extra_origins_setting(make_settings: SettingsFactory) -> None:
    settings = make_settings(ws_extra_origins="https://app.example.com")
    async with Harness(settings) as h, Live(h) as lv:
        async with lv.connect(origin="https://app.example.com") as c:
            await c.send(op="ping")
            await c.recv_type("pong")


async def test_cookie_with_foreign_origin_is_still_refused(live: Live) -> None:
    """Cross-site WebSocket hijacking: the browser would attach the cookie; the Origin check
    must stop it anyway."""
    s = await login(live.h, KEY_ALICE)
    with pytest.raises(InvalidStatus):
        async with live.connect(origin="http://evil.example", cookie=s.headers["cookie"]):
            pass


# ------------------------------------------------------------------------------ user channels
async def test_user_channel_only_for_the_signed_in_address(live: Live) -> None:
    alice = await login(live.h, KEY_ALICE)
    bob = await login(live.h, KEY_BOB)
    a_chan = f"user:{alice.address.lower()}"
    b_chan = f"user:{bob.address.lower()}"
    async with live.connect(cookie=alice.headers["cookie"]) as c:
        await c.send(op="subscribe", channels=[b_chan], id="x")
        err = await c.recv_type("error")
        assert (err["code"], err["channel"], err["ref"]) == ("forbidden", b_chan, "x")
        sub = await c.subscribe(a_chan.upper().replace("USER", "user"))  # case-insensitive
        assert sub["channels"][0]["channel"] == a_chan

        await pub(live.h, b_chan, "order", payload="bob-only")
        await pub(live.h, a_chan, "order", mine=True)
        got = await c.recv_type("order")
        assert got["data"] == {"mine": True}  # bob's message never arrives


async def test_user_channel_requires_login(live: Live) -> None:
    async with live.connect() as c:
        await c.send(op="subscribe", channels=[f"user:{'0x' + '11' * 20}"])
        assert (await c.recv_type("error"))["code"] == "unauthorized"
        # public channels still work anonymously
        assert (await c.subscribe(PRICE))["channels"][0]["channel"] == PRICE


async def test_invalid_cookie_is_treated_as_anonymous(live: Live) -> None:
    async with live.connect(cookie="wattswap_session=garbage") as c:
        await c.send(op="subscribe", channels=[f"user:{'0x' + '11' * 20}"])
        assert (await c.recv_type("error"))["code"] == "unauthorized"


# ----------------------------------------------------------------------------------- session end
async def test_logout_closes_the_users_sockets_4401(live: Live) -> None:
    alice = await login(live.h, KEY_ALICE)
    bob = await login(live.h, KEY_BOB)
    async with (
        live.connect(cookie=alice.headers["cookie"]) as a1,
        live.connect(cookie=alice.headers["cookie"]) as a2,
        live.connect(cookie=bob.headers["cookie"]) as b,
    ):
        for c in (a1, a2, b):
            await c.send(op="ping")
            await c.recv_type("pong")
        r = await live.h.client.post("/api/v1/auth/logout", headers=alice.headers)
        assert r.status_code == 204
        assert await a1.expect_close() == 4401
        assert await a2.expect_close() == 4401
        await b.send(op="ping")  # bob is unaffected
        await b.recv_type("pong")


async def test_revoked_token_cannot_open_a_socket_as_user(live: Live) -> None:
    alice = await login(live.h, KEY_ALICE)
    await live.h.client.post("/api/v1/auth/logout", headers=alice.headers)
    async with live.connect(cookie=alice.headers["cookie"]) as c:
        await c.send(op="subscribe", channels=[f"user:{alice.address.lower()}"])
        assert (await c.recv_type("error"))["code"] == "unauthorized"


async def test_token_expiry_closes_the_socket_4401(live: Live) -> None:
    s = live.h.settings
    token, claims = issue_token(s, KEY_ALICE_ADDRESS, now=int(time.time()) - s.jwt_ttl_seconds + 2)
    assert claims.expires_at - time.time() < 3
    async with live.connect(cookie=f"{s.session_cookie_name}={token}") as c:
        await c.send(op="ping")
        await c.recv_type("pong")
        started = time.monotonic()
        assert await c.expect_close(8) == 4401
        assert time.monotonic() - started < 4


async def test_revocation_backstop_poll_closes_when_the_control_message_is_lost(
    make_settings: SettingsFactory,
) -> None:
    from app.auth.tokens import decode_token, revoke

    async with Harness(make_settings(ws_ping_seconds=0.3, ws_denylist_poll_seconds=0.3)) as h:
        async with Live(h) as lv:
            alice = await login(h, KEY_ALICE)
            claims = decode_token(h.settings, alice.cookie)
            assert claims is not None
            async with lv.connect(cookie=alice.headers["cookie"]) as c:
                # denylist written WITHOUT the pub/sub announcement
                await revoke(h.app.state.redis, claims)

                async def keep_alive() -> None:
                    while True:
                        await c.send(op="pong")
                        await asyncio.sleep(0.1)

                task = asyncio.create_task(keep_alive())
                try:
                    assert await c.expect_close(5) == 4401
                finally:
                    task.cancel()


# -------------------------------------------------------------------------------------- CORS
async def test_snapshot_seq_header_is_readable_cross_origin(live: Live) -> None:
    """A browser may read a response header from another origin only if the response carries
    Access-Control-Allow-Origin for that origin AND lists the header in
    Access-Control-Expose-Headers. Assert both on every endpoint that returns X-Channel-Seq."""
    alice = await login(live.h, KEY_ALICE)
    await pub(live.h, f"trades:{MID}", "trade", t=1)
    async with httpx.AsyncClient(base_url=live.http) as client:
        for path, extra in [
            (f"/api/v1/markets/{MID}/trades", {}),
            ("/api/v1/orders", alice.headers),
            ("/api/v1/trades", alice.headers),
        ]:
            r = await client.get(path, headers={"Origin": WEB_ORIGIN, **extra})
            assert r.status_code == 200, (path, r.text)
            assert r.headers["access-control-allow-origin"] == WEB_ORIGIN
            assert r.headers["access-control-allow-credentials"] == "true"
            exposed = {
                h.strip().lower() for h in r.headers["access-control-expose-headers"].split(",")
            }
            assert "x-channel-seq" in exposed, path
            assert r.headers["x-channel-seq"].isdigit()
        trades = await client.get(f"/api/v1/markets/{MID}/trades")
        assert trades.headers["x-channel-seq"] == "1"
        # an unlisted origin gets no CORS grant, so a browser would not expose anything
        r = await client.get(
            f"/api/v1/markets/{MID}/trades", headers={"Origin": "http://evil.example"}
        )
        assert "access-control-allow-origin" not in r.headers


KEY_ALICE_ADDRESS = "0x9965507D1a55bcC2695C58ba16FB37d819B0A4dc"  # anvil account #5
