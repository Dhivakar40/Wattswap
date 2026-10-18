from eth_account import Account
from web3 import Web3

from tests.conftest import KEY_ALICE, KEY_BOB, Harness, SettingsFactory
from tests.helpers import login, new_meter, registry_contract


async def test_register_meter_end_to_end(harness: Harness, web3_sync: Web3) -> None:
    alice = await login(harness, KEY_ALICE)
    meter, device_key = await new_meter(harness, alice, capacity_w=4200, lat=13.0827, lon=80.2707)

    assert meter["register_status"] == "confirmed"
    assert meter["register_tx_hash"].startswith("0x")
    assert meter["is_simulated"] is True and meter["active"] is True
    assert meter["owner"] == Account.from_key(KEY_ALICE).address
    assert meter["device_signer"] == Account.from_key(device_key).address
    assert meter["minted_wh"] == 0

    on_chain = registry_contract(harness, web3_sync).functions.getMeter(meter["meter_id"]).call()
    owner, signer, capacity, lat_e6, lon_e6, active = on_chain
    assert owner == Account.from_key(KEY_ALICE).address
    assert signer == Account.from_key(device_key).address
    assert (capacity, lat_e6, lon_e6, active) == (4200, 13_082_700, 80_270_700, True)


async def test_register_requires_auth_and_validates_input(harness: Harness) -> None:
    body = {
        "label": "x",
        "device_signer": Account.create().address,
        "rated_capacity_w": 1000,
        "lat": 1.0,
        "lon": 1.0,
    }
    assert (await harness.client.post("/api/v1/meters", json=body)).status_code == 401

    alice = await login(harness, KEY_ALICE)
    for patch in (
        {"device_signer": "0x123"},
        {"device_signer": "not-an-address"},
        {"rated_capacity_w": 0},
        {"lat": 91},
        {"lon": -181},
        {"label": ""},
        {"unexpected": True},
    ):
        r = await harness.client.post(
            "/api/v1/meters", json={**body, **patch}, headers=alice.headers
        )
        assert r.status_code == 422, patch
        assert r.json()["error"]["code"] == "validation_error"


async def test_owners_are_isolated_and_admin_sees_all(make_settings: SettingsFactory) -> None:
    admin_addr = Account.from_key(KEY_BOB).address
    async with Harness(make_settings(admin_addresses=admin_addr)) as h:
        alice = await login(h, KEY_ALICE)
        meter, _ = await new_meter(h, alice)

        # A different, non-admin user cannot see or touch Alice's meter. 404, not 403.
        carol_key = "0x" + "07" * 32
        carol = await login(h, carol_key)
        assert (await h.client.get("/api/v1/meters", headers=carol.headers)).json() == []
        for ref in (meter["id"], meter["meter_id"]):
            assert (
                await h.client.get(f"/api/v1/meters/{ref}", headers=carol.headers)
            ).status_code == 404
        patched = await h.client.patch(
            f"/api/v1/meters/{meter['id']}", json={"label": "pwned"}, headers=carol.headers
        )
        assert patched.status_code == 404
        assert (
            await h.client.get(f"/api/v1/meters/{meter['id']}/readings", headers=carol.headers)
        ).status_code == 404

        # The owner can, by uuid or bytes32 id; the admin allow-list wallet sees everything.
        mine = await h.client.get("/api/v1/meters", headers=alice.headers)
        assert [m["meter_id"] for m in mine.json()] == [meter["meter_id"]]
        assert (
            await h.client.get(f"/api/v1/meters/{meter['meter_id']}", headers=alice.headers)
        ).status_code == 200
        admin = await login(h, KEY_BOB)
        seen = await h.client.get("/api/v1/meters", headers=admin.headers)
        assert [m["meter_id"] for m in seen.json()] == [meter["meter_id"]]


async def test_patch_label_active_and_device_signer(harness: Harness, web3_sync: Web3) -> None:
    alice = await login(harness, KEY_ALICE)
    meter, _ = await new_meter(harness, alice)
    url = f"/api/v1/meters/{meter['id']}"
    registry = registry_contract(harness, web3_sync)

    r = await harness.client.patch(url, json={"label": "roof"}, headers=alice.headers)
    assert r.status_code == 200 and r.json()["label"] == "roof"

    off = await harness.client.patch(url, json={"active": False}, headers=alice.headers)
    assert off.status_code == 200 and off.json()["active"] is False
    assert registry.functions.getMeter(meter["meter_id"]).call()[5] is False
    on = await harness.client.patch(url, json={"active": True}, headers=alice.headers)
    assert on.json()["active"] is True
    assert registry.functions.getMeter(meter["meter_id"]).call()[5] is True

    new_signer = Account.create().address
    rot = await harness.client.patch(url, json={"device_signer": new_signer}, headers=alice.headers)
    assert rot.status_code == 200 and rot.json()["device_signer"] == new_signer
    assert registry.functions.getMeter(meter["meter_id"]).call()[1] == new_signer


async def test_ready_and_public_config(harness: Harness) -> None:
    r = await harness.client.get("/ready")
    assert r.status_code == 200, r.text
    assert r.json()["checks"] == {"postgres": "ok", "redis": "ok", "chain": "ok"}
    cfg = (await harness.client.get("/api/v1/config")).json()
    assert cfg["chain_id"] == 31337 and cfg["simulated_meters"] is True
    assert cfg["reading_interval_seconds"] == 900
    assert (cfg["min_interval_seconds"], cfg["max_interval_seconds"], cfg["safety_bps"]) == (
        60,
        86_400,
        12_000,
    )
    assert "simulated" in cfg["note"].lower()
    metrics = await harness.client.get("/metrics")
    assert "wattswap_http_requests_total" in metrics.text
