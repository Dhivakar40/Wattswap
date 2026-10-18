import time
from typing import Any

import httpx
from eth_account import Account
from meter_sim.signing import SignedReading, sign_with_id
from sqlalchemy import func, select

from app.models import Mint, MintStatus, Reading
from tests.conftest import KEY_ALICE, Harness, SettingsFactory
from tests.helpers import (
    INTERVAL,
    login,
    mint_rows,
    new_meter,
    past_start,
    post_reading,
    signed_reading,
)


async def _setup(h: Harness, capacity_w: int = 5000) -> tuple[dict[str, Any], str]:
    alice = await login(h, KEY_ALICE)
    meter, key = await new_meter(h, alice, capacity_w=capacity_w)
    return meter, key


def code(resp: httpx.Response) -> str:
    return str(resp.json()["error"]["code"])


async def test_valid_reading_is_accepted_and_queues_exactly_one_mint(harness: Harness) -> None:
    meter, key = await _setup(harness)
    r = signed_reading(harness, meter, key, past_start(), wh=500)
    resp = await post_reading(harness, r)
    assert resp.status_code == 202, resp.text
    body = resp.json()
    assert body["duplicate"] is False
    assert body["reading"]["reading_id"] == r.reading_id
    assert body["reading"]["mint"]["status"] == "pending"
    rows = await mint_rows(harness)
    assert len(rows) == 1 and rows[0][1].status == MintStatus.PENDING


async def test_resending_the_same_reading_is_idempotent(harness: Harness) -> None:
    meter, key = await _setup(harness)
    r = signed_reading(harness, meter, key, past_start())
    first = await post_reading(harness, r)
    for _ in range(3):
        again = await post_reading(harness, r)
        assert again.status_code == 200 and again.json()["duplicate"] is True
    assert first.status_code == 202
    async with harness.app.state.sessionmaker() as s:
        assert await s.scalar(select(func.count()).select_from(Reading)) == 1
        assert await s.scalar(select(func.count()).select_from(Mint)) == 1


async def test_same_reading_id_with_different_data_conflicts(harness: Harness) -> None:
    meter, key = await _setup(harness)
    start = past_start()
    first = signed_reading(harness, meter, key, start, wh=500)
    assert (await post_reading(harness, first)).status_code == 202
    # The device (validly) signs a different amount for the same interval/id.
    forged = sign_with_id(
        key,
        31337,
        harness.app.state.chain.client.token_address,
        meter["meter_id"],
        first.reading_id,
        501,
        start,
        INTERVAL,
    )
    resp = await post_reading(harness, forged)
    assert resp.status_code == 409 and code(resp) == "reading_conflict"


async def test_signature_failures(harness: Harness) -> None:
    meter, key = await _setup(harness)
    start = past_start()
    good = signed_reading(harness, meter, key, start)

    wrong_key = signed_reading(harness, meter, "0x" + Account.create().key.hex(), start)
    assert code(await post_reading(harness, wrong_key)) == "invalid_signature"

    tampered = SignedReading(**{**good.__dict__, "amount_wh": good.amount_wh + 1})
    resp = await post_reading(harness, tampered)
    assert resp.status_code == 401 and code(resp) == "invalid_signature"

    garbage = SignedReading(**{**good.__dict__, "signature": "0x" + "00" * 65})
    assert (await post_reading(harness, garbage)).status_code == 401
    assert await mint_rows(harness) == []


async def test_input_validation(harness: Harness) -> None:
    meter, key = await _setup(harness)
    good = signed_reading(harness, meter, key, past_start())
    base = good.to_json()
    for patch in (
        {"amount_wh": 0},
        {"amount_wh": -5},
        {"meter_id": "0x1234"},
        {"signature": "0x12"},
        {"interval_seconds": 0},
        {"extra": 1},
    ):
        resp = await harness.client.post("/api/v1/readings", json={**base, **patch})
        assert resp.status_code == 422, patch

    bad_id = SignedReading(**{**good.__dict__, "reading_id": "0x" + "ab" * 32})
    resp = await post_reading(harness, bad_id)
    assert resp.status_code == 422 and code(resp) == "reading_id_mismatch"


async def test_unknown_meter(harness: Harness) -> None:
    meter, key = await _setup(harness)
    ghost = dict(meter, meter_id="0x" + "cd" * 32)
    resp = await post_reading(harness, signed_reading(harness, ghost, key, past_start()))
    assert resp.status_code == 404 and code(resp) == "unknown_meter"


async def test_plausibility_uses_the_readings_own_interval_length(harness: Harness) -> None:
    meter, key = await _setup(harness, capacity_w=5000)  # ceiling = 5000 W x s x 1.2 / 3600
    start = past_start()

    # 60 s: ceiling 100 Wh. 101 is refused, 100 accepted (boundary).
    over = await post_reading(harness, signed_reading(harness, meter, key, start, 101, 60))
    assert over.status_code == 422 and code(over) == "implausible_reading"
    assert over.json()["error"]["details"] == {"max_wh": 100, "amount_wh": 101}
    assert (
        await post_reading(harness, signed_reading(harness, meter, key, start, 100, 60))
    ).status_code == 202

    # 900 s: ceiling 1500 Wh (more than 15x what the 60 s slot allowed).
    mid = start + 60
    assert code(
        await post_reading(harness, signed_reading(harness, meter, key, mid, 1501, 900))
    ) == ("implausible_reading")
    assert (
        await post_reading(harness, signed_reading(harness, meter, key, mid, 1500, 900))
    ).status_code == 202

    # 3600 s: ceiling 6000 Wh.
    hour = mid + 900
    assert code(
        await post_reading(harness, signed_reading(harness, meter, key, hour, 6001, 3600))
    ) == ("implausible_reading")
    assert (
        await post_reading(harness, signed_reading(harness, meter, key, hour, 6000, 3600))
    ).status_code == 202


async def test_interval_bounds_and_completeness(harness: Harness) -> None:
    meter, key = await _setup(harness)
    short = await post_reading(harness, signed_reading(harness, meter, key, past_start(), 1, 59))
    assert short.status_code == 422 and code(short) == "invalid_interval"
    long_ = await post_reading(
        harness, signed_reading(harness, meter, key, past_start(500), 1, 86_401)
    )
    assert code(long_) == "invalid_interval"

    future = int(time.time()) - INTERVAL // 2  # ends in the future
    resp = await post_reading(harness, signed_reading(harness, meter, key, future, 10))
    assert resp.status_code == 422 and code(resp) == "interval_not_complete"


async def test_readings_must_arrive_in_order_without_overlap(harness: Harness) -> None:
    meter, key = await _setup(harness)
    t = past_start(20)

    async def send(start: int, secs: int = INTERVAL) -> httpx.Response:
        resp: httpx.Response = await post_reading(
            harness, signed_reading(harness, meter, key, start, 100, secs)
        )
        return resp

    assert (await send(t + 2 * INTERVAL)).status_code == 202
    # Earlier, non-overlapping interval after a later one: out of order.
    late = await send(t)
    assert late.status_code == 409 and code(late) == "interval_out_of_order"
    assert late.json()["error"]["details"]["last_interval_end"] == t + 3 * INTERVAL
    # Overlapping by one second.
    overlap = await send(t + 3 * INTERVAL - 1)
    assert code(overlap) == "interval_out_of_order"
    # Equal boundary is fine; so is a gap.
    assert (await send(t + 3 * INTERVAL)).status_code == 202
    assert (await send(t + 10 * INTERVAL)).status_code == 202


async def test_inactive_meter_rejected_and_rotated_key_takes_effect(harness: Harness) -> None:
    alice = await login(harness, KEY_ALICE)
    meter, old_key = await new_meter(harness, alice)
    url = f"/api/v1/meters/{meter['id']}"
    t = past_start(30)

    await harness.client.patch(url, json={"active": False}, headers=alice.headers)
    off = await post_reading(harness, signed_reading(harness, meter, old_key, t))
    assert off.status_code == 409 and code(off) == "meter_inactive"
    await harness.client.patch(url, json={"active": True}, headers=alice.headers)

    new_key = "0x" + Account.create().key.hex()
    await harness.client.patch(
        url, json={"device_signer": Account.from_key(new_key).address}, headers=alice.headers
    )
    stale = await post_reading(harness, signed_reading(harness, meter, old_key, t))
    assert stale.status_code == 401 and code(stale) == "invalid_signature"
    fresh = await post_reading(harness, signed_reading(harness, meter, new_key, t))
    assert fresh.status_code == 202


async def test_per_meter_rate_limit(make_settings: SettingsFactory) -> None:
    async with Harness(make_settings(rate_limit_meter_ingest=2)) as h:
        meter, key = await _setup(h)
        statuses = [
            (await post_reading(h, signed_reading(h, meter, key, past_start(30 - i)))).status_code
            for i in range(4)
        ]
        assert statuses == [202, 202, 429, 429]


async def test_reading_and_mint_history_endpoints_are_paginated_and_private(
    harness: Harness,
) -> None:
    alice = await login(harness, KEY_ALICE)
    meter, key = await new_meter(harness, alice)
    t = past_start(30)
    for i in range(5):
        r = await post_reading(
            harness, signed_reading(harness, meter, key, t + i * INTERVAL, 100 + i)
        )
        assert r.status_code == 202

    url = f"/api/v1/meters/{meter['id']}/readings"
    page1 = (await harness.client.get(url, params={"limit": 2}, headers=alice.headers)).json()
    assert [i["amount_wh"] for i in page1["items"]] == [104, 103]  # newest first
    page2 = (
        await harness.client.get(
            url, params={"limit": 2, "before": page1["next_before"]}, headers=alice.headers
        )
    ).json()
    assert [i["amount_wh"] for i in page2["items"]] == [102, 101]
    last = (
        await harness.client.get(
            url, params={"limit": 2, "before": page2["next_before"]}, headers=alice.headers
        )
    ).json()
    assert [i["amount_wh"] for i in last["items"]] == [100] and last["next_before"] is None

    mints = (await harness.client.get("/api/v1/mints", headers=alice.headers)).json()
    assert len(mints["items"]) == 5 and all(
        m["mint"]["status"] == "pending" for m in mints["items"]
    )

    stranger = await login(harness, "0x" + "09" * 32)
    assert (await harness.client.get("/api/v1/mints", headers=stranger.headers)).json()[
        "items"
    ] == []
    assert (await harness.client.get(url, headers=stranger.headers)).status_code == 404
    harness.client.cookies.clear()  # the jar still holds the last login cookie
    assert (await harness.client.get("/api/v1/mints")).status_code == 401


async def test_concurrent_duplicates_create_exactly_one_reading_and_one_mint(
    harness: Harness,
) -> None:
    """A retrying device (or an attacker) firing the same reading in parallel cannot double-queue."""
    import asyncio

    meter, key = await _setup(harness)
    r = signed_reading(harness, meter, key, past_start(), 500)
    responses = await asyncio.gather(*[post_reading(harness, r) for _ in range(8)])
    assert sorted(x.status_code for x in responses) == [200] * 7 + [202]
    rows = await mint_rows(harness)
    assert len(rows) == 1 and rows[0][1].status == MintStatus.PENDING
