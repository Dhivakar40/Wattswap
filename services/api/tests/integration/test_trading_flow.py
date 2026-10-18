"""END-TO-END on Anvil: two wallets, crossing orders, atomic settle, DB consistent with the chain."""

import pytest
from web3 import Web3

from app.models import OrderStatus, SettlementStatus
from tests.conftest import Harness, SettingsFactory
from tests.trading_kit import (
    MID,
    Chain,
    build,
    new_trader,
    order_row,
    post_quote,
    rows,
    seed_market,
    settle_all,
    settlements,
    trades,
    trading_settings,
)

pytestmark = pytest.mark.integration
QUOTE = 150_000  # 0.15 mUSDC per kWh


async def test_crossing_orders_settle_atomically_and_the_db_matches_the_chain(
    make_settings: SettingsFactory, chain: dict[str, str], web3_sync: Web3
) -> None:
    async with Harness(trading_settings(make_settings, chain)) as h:
        c = Chain(h, web3_sync)
        await seed_market(h)
        seller = await new_trader(h, c, watt=5_000)
        buyer = await new_trader(h, c, usdc=10_000_000)
        await post_quote(h, c, QUOTE)

        sell_hash = await seller.place_ok("sell", 2_000, 140_000)
        buy_hash = await buyer.place_ok("buy", 2_000, 160_000)
        b = build(h)
        await settle_all(b, c)

        (st,) = await settlements(h)
        (tr,) = await trades(h)
        assert st.status == SettlementStatus.CONFIRMED
        assert st.failure_reason is None and tr.tx_hash is not None
        # the exec price is the reference price, clamped into [140k, 160k] => 150k
        assert int(tr.exec_price) == QUOTE
        cost = 2_000 * QUOTE // 1000  # 300_000 micro-mUSDC = 0.30 mUSDC
        fee = cost * 100 // 10_000  # 1% protocol fee
        assert (int(tr.fill_wh), int(tr.cost), int(tr.fee)) == (2_000, cost, fee)
        # balances moved on the real chain, atomically: WATT to the buyer, mUSDC (less fee) to the seller
        assert c.balance_watt(buyer.address) == 2_000 and c.balance_watt(seller.address) == 3_000
        assert c.balance_usdc(seller.address) == cost - fee
        assert c.balance_usdc(buyer.address) == 10_000_000 - cost
        # DB agrees with the exchange's own accounting
        for hash_, who in ((sell_hash, seller), (buy_hash, buyer)):
            o = await order_row(h, hash_)
            assert o.status == OrderStatus.FILLED and int(o.filled_wh) == 2_000
            assert int(o.reserved_wh) == 0
            assert c.exchange.functions.filled(hash_).call() == 2_000
            api = await who.order(hash_)
            assert api["status"] == "filled" and api["remaining_wh"] == "0"
        # public tape shows the confirmed trade; the book is empty
        tape = (await h.client.get(f"/api/v1/markets/{MID}/trades")).json()
        assert [t["fill_wh"] for t in tape] == ["2000"] and tape[0]["status"] == "confirmed"
        book = (await h.client.get(f"/api/v1/markets/{MID}/book")).json()
        assert book["bids"] == [] and book["asks"] == []
        assert len(await rows(h, type(st))) == 1
