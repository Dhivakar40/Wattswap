"""/portfolio equals the sums over the FULL paginated /trades and /mints lists (> 200 trades)."""

import secrets
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from eth_account import Account
from sqlalchemy import select

from app.models import (
    Meter,
    Mint,
    MintStatus,
    Order,
    OrderSide,
    Reading,
    RegisterStatus,
    Settlement,
    SettlementStatus,
    Trade,
    User,
)
from tests.conftest import KEY_ALICE, Harness, SettingsFactory
from tests.helpers import login

MARKET = "0x" + "11" * 32


def _hash() -> str:
    return "0x" + secrets.token_hex(32)


def _order(maker: User, side: OrderSide) -> Order:
    return Order(
        order_hash=_hash(),
        maker_id=maker.id,
        maker=maker.address,
        market_id=MARKET,
        side=side,
        amount_wh=Decimal(10**9),
        limit_price=Decimal(100_000),
        expiry=2**40,
        nonce=Decimal(secrets.randbelow(10**9)),
        salt=_hash(),
        signature="0x00",
    )


async def _all_trades(h: Harness, headers: dict[str, str]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    before: str | None = None
    while True:
        params: dict[str, Any] = {"limit": 50}
        if before:
            params["before"] = before
        page = (await h.client.get("/api/v1/trades", params=params, headers=headers)).json()
        if not page:
            return out
        out += page
        before = page[-1]["created_at"]


async def _all_mints(h: Harness, headers: dict[str, str]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    before: str | None = None
    while True:
        params: dict[str, Any] = {"limit": 40}
        if before:
            params["before"] = before
        page = (await h.client.get("/api/v1/mints", params=params, headers=headers)).json()
        out += page["items"]
        before = page["next_before"]
        if before is None:
            return out


async def test_totals_equal_the_sum_over_the_full_paginated_lists(
    make_settings: SettingsFactory,
) -> None:
    async with Harness(make_settings()) as h:
        me = await login(h, KEY_ALICE)
        mine_addr = me.address.lower()
        other_addr = Account.create().address.lower()
        now = datetime.now(UTC)
        async with h.app.state.sessionmaker() as s, s.begin():
            user = await s.scalar(select(User).where(User.address == mine_addr))
            assert user is not None
            other = User(address=other_addr)
            s.add(other)
            await s.flush()
            buy_me, sell_me = _order(user, OrderSide.BUY), _order(user, OrderSide.SELL)
            buy_ot, sell_ot = _order(other, OrderSide.BUY), _order(other, OrderSide.SELL)
            s.add_all([buy_me, sell_me, buy_ot, sell_ot])
            await s.flush()

            states: list[SettlementStatus | None] = [
                *([SettlementStatus.CONFIRMED] * 5),
                SettlementStatus.FAILED,
                SettlementStatus.SUBMITTED,
                SettlementStatus.MATCHED,
                None,  # indexed from chain, no settlement row => confirmed
            ]
            for i in range(270):  # > 200 so a single capped page cannot cover them
                sold = i % 2 == 0
                when = now - timedelta(hours=i * 3)  # ~34 days: exercises every period window
                fill, price = 1_000 + i, 100_000 + i
                cost = fill * price // 1000
                t = Trade(
                    id=uuid.uuid4(),
                    market_id=MARKET,
                    buy_order_id=(buy_ot if sold else buy_me).id,
                    sell_order_id=(sell_me if sold else sell_ot).id,
                    buyer=other_addr if sold else mine_addr,
                    seller=mine_addr if sold else other_addr,
                    fill_wh=Decimal(fill),
                    exec_price=Decimal(price),
                    cost=Decimal(cost),
                    fee=Decimal(cost * 100 // 10_000),
                    executed_at=when,
                    created_at=when,
                    external=False,
                )
                st = states[i % len(states)]
                s.add(t)
                if st is None:
                    t.external = True
                else:
                    await s.flush()
                    s.add(
                        Settlement(
                            trade_id=t.id,
                            market_id=MARKET,
                            buy_hash=_hash(),
                            sell_hash=_hash(),
                            status=st,
                            provisional_exec_price=Decimal(price),
                            failure_reason="x" if st == SettlementStatus.FAILED else None,
                        )
                    )

            meter = Meter(
                meter_id=_hash(),
                owner_id=user.id,
                device_signer=other_addr,
                label="m",
                rated_capacity_w=5000,
                lat_e6=0,
                lon_e6=0,
                register_status=RegisterStatus.CONFIRMED,
            )
            s.add(meter)
            await s.flush()
            mint_states = [MintStatus.CONFIRMED] * 4 + [MintStatus.PENDING, MintStatus.FAILED]
            for i in range(230):
                when = now - timedelta(hours=i * 4)
                reading = Reading(
                    reading_id=_hash(),
                    meter_pk=meter.id,
                    amount_wh=500 + i,
                    interval_start=1_700_000_000 + i * 900,
                    interval_seconds=900,
                    signature="0x" + "00" * 65,
                    received_at=when,
                )
                s.add(reading)
                await s.flush()
                ms = mint_states[i % len(mint_states)]
                s.add(
                    Mint(
                        reading_pk=reading.id,
                        status=ms,
                        attempts=1,
                        created_at=when,
                        confirmed_at=when if ms == MintStatus.CONFIRMED else None,
                    )
                )

        headers = me.headers
        trades = await _all_trades(h, headers)
        mints = await _all_mints(h, headers)
        assert len(trades) == 270 and len(mints) == 230

        last: dict[str, Any] = {}
        for period, hours in (("all", None), ("24h", 24), ("7d", 24 * 7), ("30d", 24 * 30)):
            r = await h.client.get("/api/v1/portfolio", params={"period": period}, headers=headers)
            assert r.status_code == 200, r.text
            p = r.json()
            since = (
                None
                if hours is None
                else datetime.fromisoformat(p["as_of"]) - timedelta(hours=hours)
            )

            def in_window(ts: str | None, since: datetime | None = since) -> bool:
                return since is None or (ts is not None and datetime.fromisoformat(ts) >= since)

            ok = [
                t
                for t in trades
                if t["status"] == "confirmed" and in_window(t["executed_at"] or t["created_at"])
            ]
            sells = [t for t in ok if t["seller"] == mine_addr]
            buys = [t for t in ok if t["buyer"] == mine_addr]
            gross = sum(int(t["cost"]) for t in sells)
            fees = sum(int(t["fee"]) for t in sells)
            produced = sum(
                m["amount_wh"]
                for m in mints
                if m["mint"]["status"] == "confirmed" and in_window(m["mint"]["confirmed_at"])
            )
            assert int(p["sold_wh"]) == sum(int(t["fill_wh"]) for t in sells), period
            assert int(p["bought_wh"]) == sum(int(t["fill_wh"]) for t in buys), period
            assert int(p["gross_sales_usdc"]) == gross and int(p["fees_usdc"]) == fees, period
            assert int(p["earned_usdc"]) == gross - fees, period
            assert int(p["spent_usdc"]) == sum(int(t["cost"]) for t in buys), period
            assert int(p["produced_wh"]) == produced, period
            assert p["confirmed_trades"] == len(ok), period
            if period == "all":
                last = p
                assert p["failed_trades"] == sum(t["status"] == "failed" for t in trades)
                assert p["pending_trades"] == sum(
                    t["status"] in ("matched", "submitted") for t in trades
                )
                assert p["failed_mints"] == sum(m["mint"]["status"] == "failed" for m in mints)
                assert p["pending_mints"] == sum(m["mint"]["status"] == "pending" for m in mints)
            else:
                assert int(p["sold_wh"]) < int(last["sold_wh"])  # windows really narrow the sum

        # summing one capped page (the API maximum is 200) would have under-reported
        capped = sum(
            int(t["fill_wh"])
            for t in trades[:200]
            if t["seller"] == mine_addr and t["status"] == "confirmed"
        )
        assert 0 < capped < int(last["sold_wh"])


async def test_portfolio_requires_login_and_is_zero_for_a_new_user(
    make_settings: SettingsFactory,
) -> None:
    async with Harness(make_settings()) as h:
        assert (await h.client.get("/api/v1/portfolio")).status_code == 401
        me = await login(h, KEY_ALICE)
        p = (await h.client.get("/api/v1/portfolio", headers=me.headers)).json()
        assert p["period"] == "all" and p["since"] is None
        amounts = [k for k in p if k.endswith(("_wh", "_usdc"))]
        assert amounts and all(p[k] == "0" for k in amounts)
        assert p["confirmed_trades"] == 0 and p["failed_mints"] == 0
        bad = await h.client.get("/api/v1/portfolio", params={"period": "1y"}, headers=me.headers)
        assert bad.status_code == 422
