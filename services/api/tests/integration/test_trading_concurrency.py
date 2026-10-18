"""Single-writer guarantees (two matchers at once), crash recovery, and the listener's
idempotency, reorg handling and reconciliation."""

import asyncio
from decimal import Decimal

import pytest
from redis.exceptions import LockError
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from web3 import Web3

from app.matching.matcher import Matcher
from app.models import ChainEvent, IndexerState, Order, OrderStatus, SettlementStatus
from app.workers.listener import CURSOR
from tests.conftest import Harness, SettingsFactory
from tests.integration.test_trading_settlement import pair, relayer_nonce, stack
from tests.trading_kit import (
    build,
    new_trader,
    order_row,
    post_quote,
    rows,
    settle_all,
    settlements,
    trades,
    wait_until,
)

pytestmark = pytest.mark.integration
QUOTE = 150_000
PAIRS = 8


async def all_orders(h: Harness) -> list[Order]:
    return await rows(h, Order)


# ------------------------------------------------------------------- two matchers at once
async def test_two_matchers_running_at_once_match_every_pair_exactly_once(
    make_settings: SettingsFactory, chain: dict[str, str], web3_sync: Web3
) -> None:
    async with stack(
        make_settings, chain, web3_sync, match_poll_ms=100, trading_leader_ttl_seconds=5
    ) as (h, c):
        sellers = [await new_trader(h, c, watt=10_000) for _ in range(PAIRS)]
        buyers = [await new_trader(h, c, usdc=50_000_000) for _ in range(PAIRS)]
        for t in sellers:
            await t.place_ok("sell", 1_000, 140_000)
        for t in buyers:
            await t.place_ok("buy", 1_000, 160_000)
        await post_quote(h, c, QUOTE)

        b1, b2 = build(h, leader_checks=True), build(h, leader_checks=True)
        stops = [asyncio.Event(), asyncio.Event()]
        tasks = [
            asyncio.create_task(b1.loops["matcher"].run(stops[0])),
            asyncio.create_task(b2.loops["matcher"].run(stops[1])),
            asyncio.create_task(b1.loops["relayer"].run(stops[0])),
            asyncio.create_task(b1.loops["listener"].run(stops[0])),
        ]
        try:

            async def settled() -> bool:
                sts = await settlements(h)
                # never more than one leader at a time
                assert int(b1.loops["matcher"].leader) + int(b2.loops["matcher"].leader) <= 1
                return len(sts) == PAIRS and all(
                    s.status == SettlementStatus.CONFIRMED for s in sts
                )

            await wait_until(settled)
        finally:
            for s in stops:
                s.set()
            await asyncio.gather(*tasks)

        assert b1.loops["matcher"].steps + b2.loops["matcher"].steps > 0
        trs = await trades(h)
        assert len(trs) == PAIRS  # each pair matched exactly once: no duplicate trades
        assert len({(t.buy_order_id, t.sell_order_id) for t in trs}) == PAIRS
        for o in await all_orders(h):
            assert o.status == OrderStatus.FILLED and int(o.filled_wh) == 1_000
            assert int(o.reserved_wh) == 0
            assert c.exchange.functions.filled(o.order_hash).call() == 1_000  # DB == chain
        assert sum(c.balance_watt(t.address) for t in buyers) == PAIRS * 1_000


async def test_a_follower_takes_over_when_the_leader_stops(
    make_settings: SettingsFactory, chain: dict[str, str], web3_sync: Web3
) -> None:
    async with stack(
        make_settings, chain, web3_sync, match_poll_ms=100, trading_leader_ttl_seconds=5
    ) as (h, c):
        b1, b2 = build(h, leader_checks=True), build(h, leader_checks=True)
        stop1, stop2, stop_rest = asyncio.Event(), asyncio.Event(), asyncio.Event()
        t1 = asyncio.create_task(b1.loops["matcher"].run(stop1))
        await wait_until(lambda: _true(b1.loops["matcher"].leader))
        t2 = asyncio.create_task(b2.loops["matcher"].run(stop2))
        rest = [
            asyncio.create_task(b1.loops["relayer"].run(stop_rest)),
            asyncio.create_task(b1.loops["listener"].run(stop_rest)),
        ]
        try:
            await asyncio.sleep(0.5)
            assert b1.loops["matcher"].leader and not b2.loops["matcher"].leader
            stop1.set()  # the leader goes away (lock released)
            await t1
            await wait_until(lambda: _true(b2.loops["matcher"].leader), within=10)
            await pair(h, c)
            await post_quote(h, c, QUOTE)

            async def done() -> bool:
                sts = await settlements(h)
                return len(sts) == 1 and sts[0].status == SettlementStatus.CONFIRMED

            await wait_until(done)  # matched by the follower
        finally:
            stop2.set()
            stop_rest.set()
            await asyncio.gather(t2, *rest)


async def _true(v: bool) -> bool:
    return v


async def test_concurrent_cycles_cannot_double_reserve_even_without_the_lock(
    make_settings: SettingsFactory, chain: dict[str, str], web3_sync: Web3
) -> None:
    """Both matchers run with NO leader check at the same time: Postgres row locks serialise
    them, and the second one sees the first one's reservations."""
    async with stack(make_settings, chain, web3_sync) as (h, c):
        for _ in range(6):
            await pair(h, c)
        m1 = Matcher(
            h.settings, h.app.state.sessionmaker, h.app.state.redis, h.app.state.chain.client
        )
        m2 = Matcher(
            h.settings, h.app.state.sessionmaker, h.app.state.redis, h.app.state.chain.client
        )
        for _ in range(4):
            await asyncio.gather(m1.run_once(), m2.run_once())
        assert len(await settlements(h)) == 6 and len(await trades(h)) == 6
        for o in await all_orders(h):
            assert int(o.reserved_wh) == 1_000 and int(o.filled_wh) + int(o.reserved_wh) <= int(
                o.amount_wh
            )


async def test_losing_the_lock_mid_cycle_rolls_the_cycle_back(
    make_settings: SettingsFactory, chain: dict[str, str], web3_sync: Web3
) -> None:
    async with stack(make_settings, chain, web3_sync) as (h, c):
        await pair(h, c)
        b = build(h)

        async def not_leader() -> bool:
            return False

        b.matcher.still_leader = not_leader
        with pytest.raises(LockError):
            await b.matcher.run_once()
        assert await settlements(h) == [] and await trades(h) == []
        assert all(int(o.reserved_wh) == 0 for o in await all_orders(h))  # nothing leaked
        b.matcher.still_leader = None
        await b.matcher.run_once()
        assert len(await settlements(h)) == 1


async def test_the_database_itself_refuses_to_over_reserve_an_order(
    make_settings: SettingsFactory, chain: dict[str, str], web3_sync: Web3
) -> None:
    async with stack(make_settings, chain, web3_sync) as (h, c):
        _, _, sell, _ = await pair(h, c)
        async with h.app.state.sessionmaker() as s:
            with pytest.raises(IntegrityError, match="not_overfilled"):
                await s.execute(
                    text("UPDATE orders SET reserved_wh = amount_wh + 1 WHERE order_hash = :h"),
                    {"h": sell},
                )
                await s.commit()


# ------------------------------------------------------------------------ crash recovery
async def test_relayer_crash_after_signing_resends_the_same_tx_exactly_once(
    make_settings: SettingsFactory, chain: dict[str, str], web3_sync: Web3
) -> None:
    async with stack(make_settings, chain, web3_sync) as (h, c):
        _seller, _buyer, sell, _buy = await pair(h, c)
        b = build(h)
        await b.matcher.run_once()

        async def crash(_: object) -> None:
            raise RuntimeError("process killed after the tx was persisted, before it was broadcast")

        b.relayer.before_broadcast = crash
        nonce = relayer_nonce(h, c)
        await b.relayer.run_once()
        (st,) = await settlements(h)
        assert st.status == SettlementStatus.SUBMITTED and st.raw_tx  # durable, but never sent
        assert relayer_nonce(h, c) == nonce
        raw, tx_hash = st.raw_tx, st.tx_hash

        restarted = build(h, suffix="restarted")  # a brand-new process
        await restarted.relayer.run_once()
        (st,) = await settlements(h)
        assert st.raw_tx == raw and st.tx_hashes == [tx_hash]  # the SAME signed tx, not a new one

        async def mined() -> bool:  # Anvil produces the automine block asynchronously
            return relayer_nonce(h, c) == nonce + 1

        await wait_until(mined, within=10)
        await settle_all(restarted, c)
        (st,) = await settlements(h)
        assert st.status == SettlementStatus.CONFIRMED and len(await trades(h)) == 1
        assert c.exchange.functions.filled(sell).call() == 1_000  # settled once, not twice


# ------------------------------------------------------------------------------ listener
async def event_count(h: Harness) -> int:
    return len(await rows(h, ChainEvent))


async def test_listener_is_idempotent_across_replays_and_restarts(
    make_settings: SettingsFactory, chain: dict[str, str], web3_sync: Web3
) -> None:
    async with stack(make_settings, chain, web3_sync) as (h, c):
        async with h.app.state.sessionmaker() as s:
            start = (await s.get(IndexerState, CURSOR)).last_block
        _seller, _buyer, _sell, _buy = await pair(h, c)
        b = build(h)
        await settle_all(b, c)
        n = await event_count(h)
        assert n > 0
        snapshot_before = [
            (int(o.filled_wh), int(o.reserved_wh), o.status)
            for o in sorted(await all_orders(h), key=lambda o: o.seq)
        ]

        # a "restart" that forgot its cursor re-reads the whole range: every log is a duplicate
        async with h.app.state.sessionmaker() as s, s.begin():
            st = await s.get(IndexerState, CURSOR)
            assert st is not None
            st.last_block = start
        again = build(h, suffix="again")
        cycle = await again.listener.run_once()
        assert cycle.skipped_duplicates >= 1
        assert await event_count(h) == n  # nothing was inserted twice
        after = [
            (int(o.filled_wh), int(o.reserved_wh), o.status)
            for o in sorted(await all_orders(h), key=lambda o: o.seq)
        ]
        assert after == snapshot_before  # and nothing was applied twice
        assert len(await trades(h)) == 1


async def test_listener_rolls_back_a_reorged_settlement_and_reapplies_it(
    make_settings: SettingsFactory, chain: dict[str, str], web3_sync: Web3
) -> None:
    async with stack(make_settings, chain, web3_sync) as (h, c):
        _seller, buyer, sell, _buy = await pair(h, c)
        b = build(h)
        await b.matcher.run_once()
        snap = c.rpc("evm_snapshot", [])
        await b.relayer.run_once()
        c.mine()
        await b.listener.run_once()
        (st,) = await settlements(h)
        assert st.status == SettlementStatus.CONFIRMED and int(st.block_number or 0) > 0
        confirmed_hash = st.tx_hash
        assert c.balance_watt(buyer.address) == 1_000

        # the chain reorganises: the block with the settlement disappears
        assert c.rpc("evm_revert", [snap]) is True
        assert c.balance_watt(buyer.address) == 0
        cycle = await b.listener.run_once()
        assert cycle.rolled_back_to is not None
        (st,) = await settlements(h)
        assert st.status == SettlementStatus.SUBMITTED  # no longer confirmed
        s = await order_row(h, sell)
        assert (int(s.filled_wh), int(s.reserved_wh), s.status) == (0, 1_000, OrderStatus.OPEN)
        assert (await trades(h))[0].tx_hash is None
        assert not [e for e in await rows(h, ChainEvent) if e.name == "TradeSettled"]

        # the relayer notices the tx is gone and re-sends the same signed tx; it re-confirms
        await settle_all(b, c)
        (st,) = await settlements(h)
        assert st.status == SettlementStatus.CONFIRMED and st.tx_hash == confirmed_hash
        assert c.balance_watt(buyer.address) == 1_000
        assert (await order_row(h, sell)).status == OrderStatus.FILLED


async def test_reconciliation_repairs_db_drift_toward_the_chain(
    make_settings: SettingsFactory, chain: dict[str, str], web3_sync: Web3
) -> None:
    async with stack(make_settings, chain, web3_sync) as (h, c):
        seller, _buyer, _sell, _buy = await pair(h, c)
        b = build(h)
        await settle_all(b, c)
        sell2 = await seller.place_ok("sell", 500, 141_000)
        # corrupt the DB: pretend the open order is half filled and that another was never cancelled
        async with h.app.state.sessionmaker() as s, s.begin():
            o = (await s.scalars(select(Order).where(Order.order_hash == sell2))).one()
            o.filled_wh = Decimal(200)
            o.status = OrderStatus.PARTIALLY_FILLED
        head = int(c.w3.eth.block_number)
        fixed = await b.listener.reconcile(head)
        assert fixed == [f"filled:{sell2}"]
        o = await order_row(h, sell2)
        assert int(o.filled_wh) == 0 and o.status == OrderStatus.OPEN

        # an on-chain cancel the listener never saw (event lost) is also repaired
        body = seller.body("sell", 400, 142_000)
        assert (await seller.place(body)).status_code == 201
        from tests.trading_kit import order_struct

        c.send(c.exchange.functions.cancel(order_struct(body)), seller.key)
        fixed = await b.listener.reconcile(int(c.w3.eth.block_number))
        assert f"cancelled:{body['_hash']}" in fixed
        x = await order_row(h, body["_hash"])
        assert (x.status, x.cancel_type) == (OrderStatus.CANCELLED, "onchain")


async def test_external_settlement_by_another_relayer_is_adopted(
    make_settings: SettingsFactory, chain: dict[str, str], web3_sync: Web3
) -> None:
    """A settle() sent by something other than this API (restored backup, second deployment)
    still updates the DB when the listener sees it."""
    async with stack(make_settings, chain, web3_sync) as (h, c):
        _seller, _buyer, sell, buy = await pair(h, c)
        b = build(h)  # note: the matcher is NOT run, so there is no settlement row
        from app.workers.relayer import order_tuple
        from tests.trading_kit import order_struct  # noqa: F401

        so, bo = await order_row(h, sell), await order_row(h, buy)
        quote = (bytes.fromhex(so.market_id[2:]), 150_000, c.block_ts())
        from eth_account import Account

        from app.pricing.quotes import PriceQuote, sign_quote
        from tests.conftest import KEY_PRICER, KEY_RELAYER

        q = PriceQuote(so.market_id, 150_000, quote[2])
        sq = sign_quote(
            KEY_PRICER, h.settings.chain_id, h.app.state.chain.client.exchange_address, q
        )
        c.send(
            c.exchange.functions.settle(
                order_tuple(bo),
                bytes.fromhex(bo.signature[2:]),
                order_tuple(so),
                bytes.fromhex(so.signature[2:]),
                1_000,
                150_000,
                quote,
                bytes.fromhex(sq.signature[2:]),
            ),
            KEY_RELAYER,
        )
        await b.listener.run_once()
        (tr,) = await trades(h)
        assert tr.external is True and tr.tx_hash is not None
        assert await settlements(h) == []
        assert (await order_row(h, sell)).status == OrderStatus.FILLED
        assert (await order_row(h, buy)).status == OrderStatus.FILLED
        _ = Account
