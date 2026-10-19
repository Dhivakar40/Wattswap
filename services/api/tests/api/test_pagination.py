"""Keyset cursors: rows that share a timestamp are never skipped or repeated across page boundaries."""

import base64
import json
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, Literal

import pytest
from eth_account import Account
from sqlalchemy import select

from app.core.errors import AppError
from app.core.pagination import (
    NEXT_CURSOR_HEADER,
    Cursor,
    decode_cursor,
    encode_cursor,
)
from app.models import (
    Meter,
    Mint,
    MintStatus,
    OrderSide,
    Reading,
    RegisterStatus,
    Trade,
    User,
)
from app.pricing.repo import DEFAULT_MARKET_ID, ensure_default_market
from tests.api.test_portfolio import _hash, _order
from tests.conftest import KEY_ALICE, Harness, SettingsFactory
from tests.helpers import login

_KEYS: tuple[Literal["time"], Literal["int"]] = ("time", "int")
SAME = datetime(2026, 10, 1, 12, 0, 0, 123456, tzinfo=UTC)  # every tied row carries exactly this


async def _collect_list(
    h: Harness, path: str, headers: dict[str, str], limit: int
) -> tuple[list[dict[str, Any]], int]:
    """Walk a bare-array list through the X-Next-Cursor header; returns (rows, pages)."""
    rows: list[dict[str, Any]] = []
    cursor: str | None = None
    pages = 0
    while True:
        params: dict[str, Any] = {"limit": limit}
        if cursor:
            params["before"] = cursor
        r = await h.client.get(path, params=params, headers=headers)
        assert r.status_code == 200, r.text
        page = r.json()
        pages += 1
        assert len(page) <= limit
        rows += page
        cursor = r.headers.get(NEXT_CURSOR_HEADER)
        if cursor is None:
            assert len(page) <= limit
            return rows, pages
        assert len(page) == limit  # a cursor is only issued when more rows exist
        assert pages < 1000


async def _seed_trades(h: Harness, me_addr: str, n: int) -> list[str]:
    other = Account.create().address.lower()
    async with h.app.state.sessionmaker() as s:
        await ensure_default_market(s)  # commits itself
    async with h.app.state.sessionmaker() as s, s.begin():
        user = await s.scalar(select(User).where(User.address == me_addr))
        assert user is not None
        peer = User(address=other)
        s.add(peer)
        await s.flush()
        buy, sell = _order(user, OrderSide.BUY), _order(peer, OrderSide.SELL)
        s.add_all([buy, sell])
        await s.flush()
        ids: list[str] = []
        for _ in range(n):
            t = Trade(
                id=uuid.uuid4(),
                market_id=DEFAULT_MARKET_ID,
                buy_order_id=buy.id,
                sell_order_id=sell.id,
                buyer=me_addr,
                seller=other,
                fill_wh=Decimal(1000),
                exec_price=Decimal(100_000),
                cost=Decimal(100_000),
                fee=Decimal(1000),
                executed_at=SAME,  # identical timestamps on purpose
                created_at=SAME,
                external=True,
            )
            s.add(t)
            ids.append(str(t.id))
        return ids


@pytest.mark.parametrize("limit", [1, 7, 21, 62, 63, 64])
async def test_my_trades_with_identical_timestamps_page_without_loss_or_repeat(
    make_settings: SettingsFactory, limit: int
) -> None:
    async with Harness(make_settings()) as h:
        me = await login(h, KEY_ALICE)
        ids = await _seed_trades(h, me.address.lower(), 63)
        rows, pages = await _collect_list(h, "/api/v1/trades", me.headers, limit)
        got = [t["id"] for t in rows]
        assert len(got) == len(set(got)) == 63, "a row was repeated or skipped"
        assert set(got) == set(ids)
        assert got == sorted(ids, reverse=True)  # total order: (created_at desc, id desc)
        assert pages == -(-63 // limit)  # ceil: no empty trailing page when 63 % limit == 0


async def test_market_tape_with_identical_executed_at_pages_without_loss_or_repeat(
    make_settings: SettingsFactory,
) -> None:
    async with Harness(make_settings()) as h:
        me = await login(h, KEY_ALICE)
        ids = await _seed_trades(h, me.address.lower(), 45)
        rows, _ = await _collect_list(h, f"/api/v1/markets/{DEFAULT_MARKET_ID}/trades", {}, 8)
        got = [t["id"] for t in rows]
        assert len(got) == len(set(got)) == 45 and set(got) == set(ids)


async def test_trades_across_mixed_ties_and_distinct_times(make_settings: SettingsFactory) -> None:
    """Several groups of tied rows interleaved with distinct ones: order is exactly (time, id) desc."""
    async with Harness(make_settings()) as h:
        me = await login(h, KEY_ALICE)
        mine = me.address.lower()
        other = Account.create().address.lower()
        async with h.app.state.sessionmaker() as s, s.begin():
            user = await s.scalar(select(User).where(User.address == mine))
            assert user is not None
            peer = User(address=other)
            s.add(peer)
            await s.flush()
            buy, sell = _order(user, OrderSide.BUY), _order(peer, OrderSide.SELL)
            s.add_all([buy, sell])
            await s.flush()
            expected: list[tuple[datetime, uuid.UUID]] = []
            for group in range(6):
                when = SAME - timedelta(seconds=group)
                for _ in range(1 if group % 2 else 11):  # tie groups of 11, singles between
                    t = Trade(
                        id=uuid.uuid4(), market_id=DEFAULT_MARKET_ID, buy_order_id=buy.id,
                        sell_order_id=sell.id, buyer=mine, seller=other, fill_wh=Decimal(1),
                        exec_price=Decimal(1), cost=Decimal(0), fee=Decimal(0),
                        executed_at=when, created_at=when, external=True,
                    )  # fmt: skip
                    s.add(t)
                    expected.append((when, t.id))
        rows, _ = await _collect_list(h, "/api/v1/trades", me.headers, 4)
        want = [str(i) for _, i in sorted(expected, reverse=True)]
        assert [t["id"] for t in rows] == want


async def test_mints_with_identical_created_at_page_without_loss_or_repeat(
    make_settings: SettingsFactory,
) -> None:
    async with Harness(make_settings()) as h:
        me = await login(h, KEY_ALICE)
        async with h.app.state.sessionmaker() as s, s.begin():
            user = await s.scalar(select(User).where(User.address == me.address.lower()))
            assert user is not None
            meter = Meter(
                meter_id=_hash(), owner_id=user.id, device_signer=Account.create().address.lower(),
                label="m", rated_capacity_w=5000, lat_e6=0, lon_e6=0,
                register_status=RegisterStatus.CONFIRMED,
            )  # fmt: skip
            s.add(meter)
            await s.flush()
            want: set[str] = set()
            for i in range(37):
                reading = Reading(
                    reading_id=_hash(), meter_pk=meter.id, amount_wh=10 + i,
                    interval_start=1_700_000_000 + i * 900, interval_seconds=900,
                    signature="0x" + "00" * 65, received_at=SAME,
                )  # fmt: skip
                s.add(reading)
                await s.flush()
                s.add(
                    Mint(
                        reading_pk=reading.id,
                        status=MintStatus.CONFIRMED,
                        attempts=1,
                        created_at=SAME,
                    )
                )
                want.add(reading.reading_id)
        for limit in (5, 6, 37, 50):
            got: list[str] = []
            cursor: str | None = None
            while True:
                params: dict[str, Any] = {"limit": limit, **({"before": cursor} if cursor else {})}
                page = (
                    await h.client.get("/api/v1/mints", params=params, headers=me.headers)
                ).json()
                got += [m["reading_id"] for m in page["items"]]
                cursor = page["next_before"]
                if cursor is None:
                    break
            assert len(got) == len(set(got)) == 37 and set(got) == want, limit
        # readings per meter (interval_start is unique per meter, the cursor still carries the id)
        got_r: list[str] = []
        cursor = None
        while True:
            params = {"limit": 9, **({"before": cursor} if cursor else {})}
            r = await h.client.get(
                f"/api/v1/meters/{meter.meter_id}/readings", params=params, headers=me.headers
            )
            assert r.status_code == 200, r.text
            page = r.json()
            got_r += [x["reading_id"] for x in page["items"]]
            cursor = page["next_before"]
            if cursor is None:
                break
        assert len(got_r) == len(set(got_r)) == 37 and set(got_r) == want


async def test_orders_page_with_an_opaque_cursor(make_settings: SettingsFactory) -> None:
    async with Harness(make_settings()) as h:
        me = await login(h, KEY_ALICE)
        async with h.app.state.sessionmaker() as s, s.begin():
            user = await s.scalar(select(User).where(User.address == me.address.lower()))
            assert user is not None
            orders = [_order(user, OrderSide.BUY) for _ in range(31)]
            s.add_all(orders)
        rows, _ = await _collect_list(h, "/api/v1/orders", me.headers, 8)
        got = [o["order_hash"] for o in rows]
        assert len(got) == len(set(got)) == 31
        assert set(got) == {o.order_hash for o in orders}


async def test_bad_cursors_are_rejected_with_a_clear_error(make_settings: SettingsFactory) -> None:
    async with Harness(make_settings()) as h:
        me = await login(h, KEY_ALICE)
        wrong_kind = encode_cursor(5, None)  # an orders cursor is not a trades cursor
        for bad in ("not-a-cursor", "2026-01-01T00:00:00Z", "", wrong_kind, "e30"):
            r = await h.client.get("/api/v1/trades", params={"before": bad}, headers=me.headers)
            assert r.status_code == 400, (bad, r.text)
            assert r.json()["error"]["code"] == "bad_cursor", bad


# ------------------------------------------------------------------------------------- the codec
def test_cursor_roundtrip_for_every_key_and_id_type() -> None:
    uid = uuid.uuid4()
    c = decode_cursor(encode_cursor(SAME, uid), key="time", id_type=uuid.UUID)
    assert c == Cursor(SAME, uid) and c.key == SAME  # microseconds survive
    assert decode_cursor(encode_cursor(1_700_000_000, uid), key="int", id_type=uuid.UUID) == Cursor(
        1_700_000_000, uid
    )
    assert decode_cursor(encode_cursor(SAME, 42), key="time", id_type=int) == Cursor(SAME, 42)
    assert decode_cursor(encode_cursor(7, None), key="int", id_type=None) == Cursor(7, None)


def test_cursor_is_opaque_urlsafe_base64() -> None:
    token = encode_cursor(SAME, uuid.uuid4())
    assert "=" not in token and "+" not in token and "/" not in token
    json.loads(
        base64.urlsafe_b64decode(token + "=" * (-len(token) % 4))
    )  # decodes, but clients must not


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "!!!",
        base64.urlsafe_b64encode(b"[]").decode(),
        base64.urlsafe_b64encode(b'{"v":2,"k":1,"i":null}').decode(),
        base64.urlsafe_b64encode(b'{"v":1,"k":"2026-01-01T00:00:00","i":null}').decode(),  # naive
        base64.urlsafe_b64encode(b'{"v":1,"k":true,"i":null}').decode(),
    ],
)
def test_malformed_cursors_raise_bad_cursor(raw: str) -> None:
    for key in _KEYS:
        with pytest.raises(AppError) as exc:
            decode_cursor(raw, key=key, id_type=None)
        assert exc.value.code == "bad_cursor" and exc.value.status_code == 400


def test_cursor_for_another_list_is_refused() -> None:
    uid = uuid.uuid4()
    with pytest.raises(AppError):
        decode_cursor(encode_cursor(SAME, uid), key="int", id_type=uuid.UUID)
    with pytest.raises(AppError):
        decode_cursor(encode_cursor(SAME, uid), key="time", id_type=int)
    with pytest.raises(AppError):
        decode_cursor(encode_cursor(SAME, uid), key="time", id_type=None)
    with pytest.raises(AppError):
        decode_cursor(encode_cursor(1, 2), key="int", id_type=uuid.UUID)
