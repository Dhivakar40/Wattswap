"""Public market endpoints and the role-gated admin pricing endpoints."""

import httpx
import respx
from eth_account import Account
from sqlalchemy import select

from app.models import PricingEvent, PricingParamChange
from app.pricing import publish, repo
from app.pricing.params import SPECS
from app.pricing.repo import DEFAULT_MARKET_ID
from tests.api.pricing_helpers import FakeClock, make_service, mock_weather, owner_with_meter
from tests.conftest import KEY_ALICE, Harness, SettingsFactory
from tests.helpers import login

MID = DEFAULT_MARKET_ID
ALICE = Account.from_key(KEY_ALICE).address


def admin_harness(make_settings: SettingsFactory, **kw: object) -> Harness:
    return Harness(make_settings(admin_addresses=ALICE.lower(), **kw))


# ====================================================================== access control
async def test_admin_endpoints_require_the_admin_role(make_settings: SettingsFactory) -> None:
    async with admin_harness(make_settings) as h:
        await login(h, KEY_ALICE)  # creates the user; the allow-list makes it an admin
        h.client.cookies.clear()
        stranger = await login(h, "0x" + "0d" * 32)
        h.client.cookies.clear()
        calls = [
            ("GET", "/api/v1/admin/pricing/config", None),
            ("PUT", "/api/v1/admin/pricing/config", {"values": {"p_base": 160000}}),
            ("GET", f"/api/v1/admin/pricing/state?market_id={MID}", None),
            ("POST", "/api/v1/admin/pricing/breaker/reset", {"market_id": MID}),
            ("GET", f"/api/v1/admin/pricing/events?market_id={MID}", None),
            ("POST", "/api/v1/admin/markets", {"name": "X"}),
            ("PATCH", f"/api/v1/admin/markets/{MID}", {"enabled": False}),
        ]
        for method, url, body in calls:
            anon = await h.client.request(method, url, json=body)
            assert anon.status_code == 401, (method, url)
            user = await h.client.request(method, url, json=body, headers=stranger.headers)
            assert user.status_code == 403 and user.json()["error"]["code"] == "forbidden", (
                method,
                url,
            )


# =========================================================================== parameters
async def test_config_lists_every_parameter_with_default_bounds_and_provenance(
    make_settings: SettingsFactory,
) -> None:
    async with admin_harness(make_settings) as h:
        admin = await login(h, KEY_ALICE)
        async with h.app.state.sessionmaker() as s:
            await repo.ensure_default_market(s)
        r = await h.client.get("/api/v1/admin/pricing/config", headers=admin.headers)
        assert r.status_code == 200
        body = r.json()
        entries = {e["key"]: e for e in body["entries"]}
        assert set(entries) == set(SPECS) and body["scope"] == "*"
        e = entries["p_base"]
        assert e["default"] == 150_000 and e["value"] == 150_000 and e["overridden"] is False
        assert (
            e["source"] == "default"
            and e["unit"] == "mUSDC µ/kWh"
            and e["minimum"]
            and e["maximum"]
        )
        assert entries["epsilon"]["default"] == "0.6" and entries["ema_alpha"]["value"] == "0.2"
        assert entries["max_step_bps"]["value"] == 25 and entries["move_bps"]["value"] == 1500
        assert entries["tick_seconds"]["editable"] is False
        assert len(entries["load_profile"]["value"]) == 24


async def test_updating_parameters_is_validated_audited_and_layered(
    make_settings: SettingsFactory,
) -> None:
    async with admin_harness(make_settings) as h:
        admin = await login(h, KEY_ALICE)
        async with h.app.state.sessionmaker() as s:
            await repo.ensure_default_market(s)

        put = await h.client.put(
            "/api/v1/admin/pricing/config",
            json={"values": {"p_base": 160_000, "epsilon": "0.5"}},
            headers=admin.headers,
        )
        assert put.status_code == 200, put.text
        entries = {e["key"]: e for e in put.json()["entries"]}
        assert entries["p_base"]["value"] == 160_000 and entries["p_base"]["source"] == "global"
        assert entries["epsilon"]["value"] == "0.5" and entries["epsilon"]["overridden"] is True
        change = entries["p_base"]["last_change"]
        assert change["by"] == ALICE.lower() and change["old"] is None and change["new"] == 160_000

        # a market-specific override layers on top of the global one
        market = await h.client.put(
            "/api/v1/admin/pricing/config",
            json={"market_id": MID, "values": {"p_base": 170_000}},
            headers=admin.headers,
        )
        m = {e["key"]: e for e in market.json()["entries"]}
        assert m["p_base"]["value"] == 170_000 and m["p_base"]["source"] == "market"
        assert m["epsilon"]["value"] == "0.5" and m["epsilon"]["source"] == "global"

        # audit trail: one row per changed key, with who and when; unchanged values add nothing
        again = await h.client.put(
            "/api/v1/admin/pricing/config",
            json={"values": {"p_base": 160_000}},
            headers=admin.headers,
        )
        assert again.status_code == 200
        async with h.app.state.sessionmaker() as s:
            audit = list(
                (await s.scalars(select(PricingParamChange).order_by(PricingParamChange.id))).all()
            )
        assert [(a.scope, a.key, a.new_value) for a in audit] == [
            ("*", "p_base", 160_000),
            ("*", "epsilon", "0.5"),
            (MID, "p_base", 170_000),
        ]
        assert all(a.changed_by == ALICE.lower() for a in audit)

        # removing an override falls back one layer
        reset = await h.client.put(
            "/api/v1/admin/pricing/config",
            json={"market_id": MID, "reset": ["p_base"]},
            headers=admin.headers,
        )
        back = {e["key"]: e for e in reset.json()["entries"]}
        assert back["p_base"]["value"] == 160_000 and back["p_base"]["source"] == "global"


async def test_invalid_updates_are_rejected_atomically(make_settings: SettingsFactory) -> None:
    async with admin_harness(make_settings) as h:
        admin = await login(h, KEY_ALICE)
        async with h.app.state.sessionmaker() as s:
            await repo.ensure_default_market(s)

        async def put(values: dict[str, object], **kw: object) -> dict[str, object]:
            r = await h.client.put(
                "/api/v1/admin/pricing/config", json={"values": values, **kw}, headers=admin.headers
            )
            assert r.status_code == 422, (values, r.text)
            assert r.json()["error"]["code"] == "validation_error"
            errors: dict[str, object] = r.json()["error"]["details"]["errors"]
            return errors

        assert "nope" in await put({"nope": 1})
        assert "p_base" in await put({"p_base": 10_000})  # not above the floor
        assert "p_base" in await put({"p_base": 700_000})  # not below the ceiling
        assert "epsilon" in await put({"epsilon": 0.6})  # a JSON float: refused, send "0.6"
        assert "epsilon" in await put({"epsilon": "9"})
        assert "ema_alpha" in await put({"ema_alpha": "0"})
        assert "load_profile" in await put({"load_profile": ["1"] * 5})
        assert "tick_seconds" in await put({"tick_seconds": 5})
        errs = await put(
            {"max_step_bps": 60}
        )  # the limiter alone could reach the breaker threshold
        assert "limiter alone could move the price" in str(errs["max_step_bps"])
        # a valid change bundled with an invalid one changes nothing
        await put({"p_base": 155_000, "epsilon": "9"})
        r = await h.client.get("/api/v1/admin/pricing/config", headers=admin.headers)
        assert {e["key"]: e for e in r.json()["entries"]}["p_base"]["value"] == 150_000
        async with h.app.state.sessionmaker() as s:
            assert (await s.scalars(select(PricingParamChange))).first() is None
        # unknown market / unknown reset key
        bad_market = await h.client.put(
            "/api/v1/admin/pricing/config",
            json={"market_id": "0x" + "ab" * 32, "values": {"p_base": 160_000}},
            headers=admin.headers,
        )
        assert bad_market.status_code == 404
        bad_reset = await h.client.put(
            "/api/v1/admin/pricing/config", json={"reset": ["nope"]}, headers=admin.headers
        )
        assert bad_reset.status_code == 422


@respx.mock
async def test_a_parameter_change_takes_effect_on_the_next_tick(
    make_settings: SettingsFactory,
) -> None:
    async with admin_harness(make_settings) as h:
        admin = await login(h, KEY_ALICE)
        clock = FakeClock()
        mock_weather(clock)
        svc = make_service(h, clock)
        [t1] = await svc.tick_all()  # no meters: the market is empty, the price is the neutral base
        assert t1.raw_price == t1.price == 150_000 and t1.drivers["price"]["neutral"] is True

        r = await h.client.put(
            "/api/v1/admin/pricing/config",
            json={"values": {"p_base": 160_000}},
            headers=admin.headers,
        )
        assert r.status_code == 200
        clock.advance(6)  # parameters are cached for a few seconds
        [t2] = await svc.tick_all()
        assert t2.raw_price == 160_000
        assert t2.price == 150_000 + 150_000 * 25 // 10_000  # the limiter still applies: +0.25 %


# ==================================================================== breaker and events
@respx.mock
async def test_admin_can_see_the_breaker_and_reset_it(make_settings: SettingsFactory) -> None:
    async with admin_harness(make_settings) as h:
        admin = await login(h, KEY_ALICE)
        assert (
            await h.client.get(
                f"/api/v1/admin/pricing/state?market_id={MID}", headers=admin.headers
            )
        ).status_code in (404,)  # nothing reported yet

        clock = FakeClock()
        route = mock_weather(clock)
        await owner_with_meter(h)
        svc = make_service(h, clock)
        await svc.poll_weather_once()
        await svc.tick_all()
        route.mock(return_value=httpx.Response(500))
        clock.advance(2000)
        await svc.poll_weather_once()
        [frozen] = await svc.tick_all()
        assert frozen.breaker.mode.value == "FROZEN"

        st = (
            await h.client.get(
                f"/api/v1/admin/pricing/state?market_id={MID}", headers=admin.headers
            )
        ).json()
        assert st["breaker"]["mode"] == "FROZEN" and st["weather"][0]["ageS"] >= 2000
        assert st["market"] == "WATT-mUSDC" and st["oracle"]["lastResult"] == "never"

        # weather recovers, but the operator does not want to wait out the hysteresis
        mock_weather(clock)
        clock.advance(120)
        await svc.poll_weather_once()
        r = await h.client.post(
            "/api/v1/admin/pricing/breaker/reset", json={"market_id": MID}, headers=admin.headers
        )
        assert r.status_code == 202 and r.json()["queued"] is True
        clock.advance(15)
        [after] = await svc.tick_all()
        assert after.breaker.mode.value == "NORMAL"  # straight to NORMAL, no RECOVERING ticks
        assert await h.app.state.redis.get(publish.command_key(MID)) is None  # consumed once

        ev = (
            await h.client.get(
                f"/api/v1/admin/pricing/events?market_id={MID}", headers=admin.headers
            )
        ).json()
        kinds = [e["kind"] for e in ev]
        assert "breaker_frozen" in kinds and "breaker_reset_requested" in kinds
        requested = next(e for e in ev if e["kind"] == "breaker_reset_requested")
        assert requested["details"]["by"] == ALICE.lower()
        async with h.app.state.sessionmaker() as s:
            assert (
                await s.scalar(select(PricingEvent).where(PricingEvent.kind == "breaker_frozen"))
            ) is not None


# ============================================================================== markets
async def test_admin_manages_markets_and_the_primary_flag(make_settings: SettingsFactory) -> None:
    async with admin_harness(make_settings) as h:
        admin = await login(h, KEY_ALICE)
        listing = (await h.client.get("/api/v1/markets")).json()  # public; creates the default
        assert [m["name"] for m in listing] == ["WATT-mUSDC"]
        assert listing[0]["is_primary"] is True and listing[0]["onchain_enabled"] is True
        assert listing[0]["region"] == {
            "min_lat_e6": None, "max_lat_e6": None, "min_lon_e6": None, "max_lon_e6": None,
        }  # fmt: skip

        r = await h.client.post(
            "/api/v1/admin/markets",
            json={
                "name": "WATT-INDIA",
                "min_lat_e6": 6_000_000,
                "max_lat_e6": 36_000_000,
                "min_lon_e6": 68_000_000,
                "max_lon_e6": 98_000_000,
            },
            headers=admin.headers,
        )
        assert r.status_code == 201, r.text
        created = r.json()
        assert created["market_id"] == repo.market_id_for("WATT-INDIA")
        assert (
            created["onchain_enabled"] is False
        )  # defined here, but not enabled on the exchange yet
        dup = await h.client.post(
            "/api/v1/admin/markets", json={"name": "WATT-INDIA"}, headers=admin.headers
        )
        assert dup.status_code == 409
        bad = await h.client.post(
            "/api/v1/admin/markets",
            json={"name": "BAD", "min_lat_e6": 10, "max_lat_e6": 5},
            headers=admin.headers,
        )
        assert bad.status_code == 422

        # only one primary (the market pushed to the single-price oracle)
        patched = await h.client.patch(
            f"/api/v1/admin/markets/{created['market_id']}",
            json={"is_primary": True},
            headers=admin.headers,
        )
        assert patched.status_code == 200 and patched.json()["is_primary"] is True
        flags = {m["name"]: m["is_primary"] for m in (await h.client.get("/api/v1/markets")).json()}
        assert flags == {"WATT-mUSDC": False, "WATT-INDIA": True}
        disabled = await h.client.patch(
            f"/api/v1/admin/markets/{created['market_id']}",
            json={"enabled": False},
            headers=admin.headers,
        )
        assert disabled.json()["enabled"] is False
        missing = await h.client.patch(
            "/api/v1/admin/markets/0x" + "ab" * 32, json={"enabled": True}, headers=admin.headers
        )
        assert missing.status_code == 404


# ============================================================================== public API
@respx.mock
async def test_public_price_ticks_and_forecast(make_settings: SettingsFactory) -> None:
    async with Harness(make_settings()) as h:
        none_yet = await h.client.get(f"/api/v1/markets/{MID}/price")
        assert none_yet.status_code == 404 and none_yet.json()["error"]["code"] in (
            "not_found",
            "no_price_yet",
        )
        assert (await h.client.get("/api/v1/markets/0x" + "ab" * 32 + "/price")).status_code == 404

        clock = FakeClock()
        mock_weather(clock)
        await owner_with_meter(h)
        svc = make_service(h, clock)
        await svc.poll_weather_once()
        for _ in range(5):
            await svc.tick_all()
            clock.advance(15)

        price = (await h.client.get(f"/api/v1/markets/{MID}/price")).json()
        assert (
            price["seq"] == 5 and isinstance(price["price"], int) and price["breaker"] == "NORMAL"
        )
        assert price["quote"]["price"] == price["price"] and price["quote"]["signature"].startswith(
            "0x"
        )
        assert price["drivers"]["supply"]["generation_kw"] == "2.828800"  # the "why", for Insights
        assert price["drivers"]["attribution"] is not None and price["accelerated"] is False

        # fall back to the database when Redis has nothing
        await h.app.state.redis.delete(publish.latest_key(MID))
        fallback = (await h.client.get(f"/api/v1/markets/{MID}/price")).json()
        assert (
            fallback["seq"] is None
            and fallback["price"] == price["price"]
            and fallback["quote"] is None
        )

        page1 = (await h.client.get(f"/api/v1/markets/{MID}/ticks", params={"limit": 2})).json()
        assert len(page1["items"]) == 2 and page1["next_before"] is not None
        assert "drivers" not in page1["items"][0]  # light by default
        page2 = (
            await h.client.get(
                f"/api/v1/markets/{MID}/ticks", params={"limit": 10, "before": page1["next_before"]}
            )
        ).json()
        assert len(page2["items"]) == 3 and page2["next_before"] is None
        assert (
            page1["items"][0]["ts"] > page1["items"][1]["ts"] > page2["items"][0]["ts"]
        )  # newest first
        full = (
            await h.client.get(
                f"/api/v1/markets/{MID}/ticks", params={"limit": 1, "drivers": "true"}
            )
        ).json()
        assert full["items"][0]["drivers"]["breaker"]["mode"] == "NORMAL"

        outlook = (await h.client.get(f"/api/v1/markets/{MID}/forecast")).json()
        assert outlook["marketId"] == MID and len(outlook["hours"]) == 24
        hour = outlook["hours"][0]
        assert set(hour) >= {
            "time",
            "ghi_wm2",
            "generation_kw",
            "supply_kw",
            "demand_kw",
            "raw_price",
            "clamps",
        }
        assert all(isinstance(x["raw_price"], int) for x in outlook["hours"])
        assert [x["time"] for x in outlook["hours"]] == sorted(x["time"] for x in outlook["hours"])


async def test_forecast_before_any_tick_is_a_clean_404(make_settings: SettingsFactory) -> None:
    async with Harness(make_settings()) as h:
        await h.client.get("/api/v1/markets")
        r = await h.client.get(f"/api/v1/markets/{MID}/forecast")
        assert r.status_code == 404 and r.json()["error"]["code"] == "no_forecast_yet"


async def test_public_market_endpoints_are_rate_limited(make_settings: SettingsFactory) -> None:
    async with Harness(make_settings(rate_limit_ip=3)) as h:
        codes = [(await h.client.get("/api/v1/markets")).status_code for _ in range(5)]
        assert codes == [200, 200, 200, 429, 429]
