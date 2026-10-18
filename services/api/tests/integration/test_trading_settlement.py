"""Matching and settlement scenarios on Anvil: partial fills, dust, expiry, self-trade, funding
failures, settle reverts returning orders to the book, cancel races, quote freshness at mining time,
release backoff."""

import asyncio
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager

import pytest
from eth_account import Account
from sqlalchemy import select
from web3 import Web3

from app.models import Order, OrderStatus, PairCooldown, SettlementStatus
from tests.conftest import Harness, SettingsFactory
from tests.trading_kit import (
    MID,
    Chain,
    Trader,
    build,
    drive,
    new_trader,
    order_row,
    order_struct,
    post_quote,
    rows,
    seed_market,
    settle_all,
    settlements,
    trades,
    trading_settings,
)

pytestmark = pytest.mark.integration
QUOTE = 150_000


@asynccontextmanager
async def stack(
    make_settings: SettingsFactory, chain: dict[str, str], w3: Web3, **kw: object
) -> AsyncIterator[tuple[Harness, Chain]]:
    async with Harness(trading_settings(make_settings, chain, **kw)) as h:
        c = Chain(h, w3)
        await seed_market(h)
        await post_quote(h, c, QUOTE)
        try:
            yield h, c
        finally:
            c.set_automine(True)


@contextmanager
def no_automine(c: Chain) -> Iterator[None]:
    c.set_automine(False)
    try:
        yield
    finally:
        c.set_automine(True)


async def pair(
    h: Harness, c: Chain, *, sell_wh: int = 1_000, buy_wh: int = 1_000
) -> tuple[Trader, Trader, str, str]:
    seller = await new_trader(h, c, watt=50_000)
    buyer = await new_trader(h, c, usdc=50_000_000)
    s = await seller.place_ok("sell", sell_wh, 140_000)
    b = await buyer.place_ok("buy", buy_wh, 160_000)
    return seller, buyer, s, b


def relayer_nonce(h: Harness, c: Chain) -> int:
    """Transactions the relayer key has sent (a pre-flight failure must not add one)."""
    return int(c.w3.eth.get_transaction_count(_relayer(h)))


def _relayer(h: Harness) -> str:
    key = h.settings.relayer_private_key
    assert key is not None
    return str(Account.from_key(key.get_secret_value()).address)


# ----------------------------------------------------------------------------- fills
async def test_partial_fills_remainders_and_dust(
    make_settings: SettingsFactory, chain: dict[str, str], web3_sync: Web3
) -> None:
    async with stack(make_settings, chain, web3_sync) as (h, c):
        seller = await new_trader(h, c, watt=10_000)
        b1 = await new_trader(h, c, usdc=50_000_000)
        b2 = await new_trader(h, c, usdc=50_000_000)
        b3 = await new_trader(h, c, usdc=50_000_000)
        sell = await seller.place_ok("sell", 3_000, 140_000)
        bundle = build(h)

        await b1.place_ok("buy", 1_000, 160_000)
        await settle_all(bundle, c)
        o = await order_row(h, sell)
        assert (o.status, int(o.filled_wh)) == (OrderStatus.PARTIALLY_FILLED, 1_000)

        await b2.place_ok("buy", 1_800, 160_000)
        await settle_all(bundle, c)
        o = await order_row(h, sell)
        assert (o.status, int(o.filled_wh)) == (OrderStatus.PARTIALLY_FILLED, 2_800)
        assert 3_000 - int(o.filled_wh) == 200  # the remainder rests on the book
        book = (await h.client.get(f"/api/v1/markets/{MID}/book")).json()
        assert book["asks"] == [{"price": "140000", "amount_wh": "200", "orders": 1}]

        # a 250 Wh bid takes the last 200 Wh; its own 50 Wh leftover is below the 100 Wh minimum
        bid = await b3.place_ok("buy", 250, 160_000)
        await settle_all(bundle, c)
        assert (await order_row(h, sell)).status == OrderStatus.FILLED
        leftover = await order_row(h, bid)
        assert (
            leftover.status == OrderStatus.CANCELLED and leftover.cancel_reason == "dust_remainder"
        )
        assert leftover.cancel_type == "offchain" and int(leftover.filled_wh) == 200
        assert c.balance_watt(b1.address) == 1_000 and c.balance_watt(b2.address) == 1_800
        assert c.balance_watt(b3.address) == 200
        assert c.exchange.functions.filled(sell).call() == 3_000
        # no order was ever over-reserved or over-filled, and nothing is left reserved
        async with h.app.state.sessionmaker() as s:
            for o in await s.scalars(select(Order)):
                assert int(o.filled_wh) + int(o.reserved_wh) <= int(o.amount_wh)
                assert int(o.reserved_wh) == 0
        assert all(st.status == SettlementStatus.CONFIRMED for st in await settlements(h))


async def test_no_gas_is_spent_on_dust_sized_fills(
    make_settings: SettingsFactory, chain: dict[str, str], web3_sync: Web3
) -> None:
    async with stack(make_settings, chain, web3_sync, min_fill_cost=5_000) as (h, c):
        await post_quote(h, c, 20_000)
        seller = await new_trader(h, c, watt=10_000)
        buyer = await new_trader(h, c, usdc=50_000_000)
        await seller.place_ok("sell", 100, 20_000)  # 100 Wh at 0.02 = cost 2_000 < min_fill_cost
        await buyer.place_ok("buy", 100, 20_000)
        nonce = relayer_nonce(h, c)
        await settle_all(build(h), c, rounds=3)
        assert await settlements(h) == [] and relayer_nonce(h, c) == nonce


# ----------------------------------------------------------------------------- expiry
async def test_expired_orders_never_match_and_an_order_expiring_before_submit_costs_no_gas(
    make_settings: SettingsFactory, chain: dict[str, str], web3_sync: Web3
) -> None:
    async with stack(make_settings, chain, web3_sync) as (h, c):
        seller = await new_trader(h, c, watt=10_000)
        buyer = await new_trader(h, c, usdc=50_000_000)
        short = await seller.place_ok("sell", 1_000, 140_000, expires_in=200)
        long_sell = await seller.place_ok("sell", 1_000, 145_000, expires_in=7_200)
        bid = await buyer.place_ok("buy", 1_000, 160_000)
        bundle = build(h)
        await bundle.matcher.run_once()  # matches the cheaper SHORT-lived sell
        assert int((await order_row(h, short)).reserved_wh) == 1_000

        # time passes before the relayer submits: the order is inside the expiry safety margin
        c.increase_time(150)
        await post_quote(h, c, QUOTE)
        nonce = relayer_nonce(h, c)
        await bundle.relayer.run_once()
        (st,) = await settlements(h)
        assert st.status == SettlementStatus.FAILED and st.failure_reason.startswith(
            "preflight:expired"
        )
        assert relayer_nonce(h, c) == nonce  # no transaction was sent
        assert int((await order_row(h, bid)).reserved_wh) == 0  # the buyer is free again

        # the expired order is marked expired, and the buyer trades with the other seller instead
        await post_quote(h, c, QUOTE)
        c.increase_time(100)
        await post_quote(h, c, QUOTE)
        await settle_all(bundle, c)
        assert (await order_row(h, short)).status == OrderStatus.EXPIRED
        assert (await order_row(h, long_sell)).status == OrderStatus.FILLED
        assert (await order_row(h, bid)).status == OrderStatus.FILLED


# ------------------------------------------------------------------------- self trade
async def test_a_maker_is_never_matched_with_themselves(
    make_settings: SettingsFactory, chain: dict[str, str], web3_sync: Web3
) -> None:
    async with stack(make_settings, chain, web3_sync) as (h, c):
        alice = await new_trader(h, c, watt=10_000, usdc=50_000_000)
        bob = await new_trader(h, c, watt=10_000)
        own_sell = await alice.place_ok("sell", 1_000, 140_000)  # best ask, but Alice's own
        bobs_sell = await bob.place_ok("sell", 1_000, 145_000)
        alice_buy = await alice.place_ok("buy", 1_000, 160_000)
        bundle = build(h)
        await settle_all(bundle, c)
        (tr,) = await trades(h)
        assert (tr.buyer, tr.seller) == (alice.address.lower(), bob.address.lower())
        assert (await order_row(h, own_sell)).status == OrderStatus.OPEN  # untouched
        assert (await order_row(h, bobs_sell)).status == OrderStatus.FILLED
        assert (await order_row(h, alice_buy)).status == OrderStatus.FILLED
        # with only her own orders left, nothing more can happen
        await alice.place_ok("buy", 500, 160_000)
        await settle_all(bundle, c, rounds=3)
        assert len(await trades(h)) == 1


# ------------------------------------------------------------------ funding after accept
async def test_sweeper_cancels_orders_that_lose_their_funding(
    make_settings: SettingsFactory, chain: dict[str, str], web3_sync: Web3
) -> None:
    async with stack(make_settings, chain, web3_sync) as (h, c):
        t = await new_trader(h, c, watt=3_000, usdc=1_000_000)
        o1 = await t.place_ok("sell", 1_000, 140_000)
        o2 = await t.place_ok("sell", 1_000, 141_000)
        o3 = await t.place_ok("sell", 1_000, 142_000)
        bid = await t.place_ok("buy", 1_000, 100_000)
        bundle = build(h)
        assert await bundle.sweeper.run_once(full=True) == set()  # all funded

        # 1_500 WATT leave the wallet: only the OLDEST order is still fully funded
        c.send(
            c.watt.functions.transfer("0x000000000000000000000000000000000000dEaD", 1_500), t.key
        )
        gone = await bundle.sweeper.run_once(full=True)
        assert gone == {o2, o3}
        assert (await order_row(h, o1)).status == OrderStatus.OPEN
        for hash_ in (o2, o3):
            o = await order_row(h, hash_)
            assert o.status == OrderStatus.CANCELLED and o.cancel_reason == "unfunded:balance"
            assert o.cancel_type == "offchain"

        # revoking the allowance voids the rest, for a different stated reason
        c.send(c.watt.functions.approve(c.exchange.address, 0), t.key)
        assert await bundle.sweeper.run_once(full=True) == {o1}
        assert (await order_row(h, o1)).cancel_reason == "unfunded:allowance"
        assert (await order_row(h, bid)).status == OrderStatus.OPEN  # the funded buy is untouched


async def test_listener_events_trigger_a_funding_recheck_without_waiting_for_the_sweep(
    make_settings: SettingsFactory, chain: dict[str, str], web3_sync: Web3
) -> None:
    async with stack(make_settings, chain, web3_sync, funding_sweep_seconds=3600.0) as (h, c):
        t = await new_trader(h, c, watt=2_000)
        o1 = await t.place_ok("sell", 1_000, 140_000)
        bundle = build(h)
        await bundle.sweeper.run_once(full=True)  # starts the 1h timer
        c.send(c.watt.functions.approve(c.exchange.address, 0), t.key)
        await bundle.listener.run_once()  # sees the Approval event => marks the maker dirty
        assert await bundle.sweeper.run_once() == {o1}  # not a full sweep, still caught


async def test_preflight_cancels_an_unfunded_order_and_returns_the_counterparty(
    make_settings: SettingsFactory, chain: dict[str, str], web3_sync: Web3
) -> None:
    async with stack(make_settings, chain, web3_sync) as (h, c):
        seller, buyer, sell, buy = await pair(h, c)
        bundle = build(h)
        await bundle.matcher.run_once()
        c.send(c.usdc.functions.approve(c.exchange.address, 0), buyer.key)  # buyer pulls funds
        nonce = relayer_nonce(h, c)
        await bundle.relayer.run_once()
        (st,) = await settlements(h)
        assert st.status == SettlementStatus.FAILED and "unfunded:allowance" in st.failure_reason
        assert relayer_nonce(h, c) == nonce
        b = await order_row(h, buy)
        assert b.status == OrderStatus.CANCELLED and b.cancel_reason == "unfunded:allowance"
        s = await order_row(h, sell)
        assert s.status == OrderStatus.OPEN and int(s.reserved_wh) == 0  # back on the book


# -------------------------------------------------------- settle revert -> back to the book
async def test_a_mined_settle_revert_returns_the_innocent_order_to_the_book(
    make_settings: SettingsFactory, chain: dict[str, str], web3_sync: Web3
) -> None:
    async with stack(make_settings, chain, web3_sync) as (h, c):
        seller, buyer, sell, buy = await pair(h, c)
        bundle = build(h)
        with no_automine(c):
            await bundle.matcher.run_once()
            await bundle.relayer.run_once()  # settle() is now in the mempool
            (st,) = await settlements(h)
            assert st.status == SettlementStatus.SUBMITTED
            # the buyer revokes the allowance with a higher tip, so it lands BEFORE the settle
            c.send_nowait(c.usdc.functions.approve(c.exchange.address, 0), buyer.key, tip_gwei=50)
            c.mine()
        assert c.receipt_status(st.tx_hash) == 0  # type: ignore[arg-type]  # the settle reverted on-chain

        await bundle.relayer.run_once()
        (st,) = await settlements(h)
        assert st.status == SettlementStatus.FAILED and st.failure_reason  # reason recorded
        s = await order_row(h, sell)
        assert s.status == OrderStatus.OPEN and int(s.reserved_wh) == 0 and int(s.filled_wh) == 0
        b = await order_row(h, buy)
        assert b.status == OrderStatus.CANCELLED and b.cancel_reason == "unfunded:allowance"
        assert c.balance_watt(seller.address) == 50_000  # nothing moved

        # the seller's order is genuinely still tradable: a funded buyer settles with it
        buyer2 = await new_trader(h, c, usdc=50_000_000)
        await buyer2.place_ok("buy", 1_000, 160_000)
        await settle_all(bundle, c)
        assert c.balance_watt(buyer2.address) == 1_000
        assert (await order_row(h, sell)).status == OrderStatus.FILLED


# ------------------------------------------------------------------------ cancel races
async def test_on_chain_cancel_before_broadcast_is_caught_without_spending_gas(
    make_settings: SettingsFactory, chain: dict[str, str], web3_sync: Web3
) -> None:
    async with stack(make_settings, chain, web3_sync) as (h, c):
        seller = await new_trader(h, c, watt=10_000)
        buyer = await new_trader(h, c, usdc=50_000_000)
        body = seller.body("sell", 1_000, 140_000)
        assert (await seller.place(body)).status_code == 201
        buy = await buyer.place_ok("buy", 1_000, 160_000)
        bundle = build(h)
        await bundle.matcher.run_once()
        c.send(c.exchange.functions.cancel(order_struct(body)), seller.key)
        nonce = relayer_nonce(h, c)
        await bundle.relayer.run_once()
        (st,) = await settlements(h)
        assert st.status == SettlementStatus.FAILED and st.failure_reason.startswith("cancel_race")
        assert relayer_nonce(h, c) == nonce
        s = await order_row(h, body["_hash"])
        assert (s.status, s.cancel_type) == (OrderStatus.CANCELLED, "onchain")
        b = await order_row(h, buy)
        assert b.status == OrderStatus.OPEN and int(b.reserved_wh) == 0


@pytest.mark.parametrize("variant", ["cancel", "cancel_up_to"])
async def test_on_chain_cancel_after_broadcast_fails_the_settlement_and_is_never_retried(
    make_settings: SettingsFactory, chain: dict[str, str], web3_sync: Web3, variant: str
) -> None:
    async with stack(make_settings, chain, web3_sync) as (h, c):
        seller = await new_trader(h, c, watt=10_000)
        buyer = await new_trader(h, c, usdc=50_000_000)
        body = seller.body("sell", 1_000, 140_000, nonce=3)
        assert (await seller.place(body)).status_code == 201
        buy = await buyer.place_ok("buy", 1_000, 160_000)
        bundle = build(h)
        cancel_fn = (
            c.exchange.functions.cancel(order_struct(body))
            if variant == "cancel"
            else c.exchange.functions.cancelUpTo(4)
        )
        with no_automine(c):
            await bundle.matcher.run_once()
            await bundle.relayer.run_once()  # submitted, not yet mined
            (st,) = await settlements(h)
            first_hash = st.tx_hash
            c.send_nowait(cancel_fn, seller.key, tip_gwei=50)  # mined first
            c.mine()
        assert c.receipt_status(first_hash) == 0  # type: ignore[arg-type]

        for _ in range(3):  # several relayer passes: it must NOT try again
            await bundle.relayer.run_once()
            await bundle.matcher.run_once()
        (st,) = await settlements(h)
        assert len(await settlements(h)) == 1  # no second attempt, no re-match
        assert st.status == SettlementStatus.FAILED and st.failure_reason.startswith("cancel_race")
        assert st.attempts == 0 and len(st.tx_hashes) == 1
        s = await order_row(h, body["_hash"])
        assert (s.status, s.cancel_type) == (OrderStatus.CANCELLED, "onchain")
        b = await order_row(h, buy)
        assert (
            b.status == OrderStatus.OPEN and int(b.reserved_wh) == 0
        )  # removed seller, buyer free
        assert c.balance_watt(buyer.address) == 0


async def test_listener_seeing_the_cancel_first_fails_the_inflight_settlement(
    make_settings: SettingsFactory, chain: dict[str, str], web3_sync: Web3
) -> None:
    async with stack(make_settings, chain, web3_sync) as (h, c):
        seller = await new_trader(h, c, watt=10_000)
        buyer = await new_trader(h, c, usdc=50_000_000)
        body = seller.body("sell", 1_000, 140_000)
        assert (await seller.place(body)).status_code == 201
        buy = await buyer.place_ok("buy", 1_000, 160_000)
        bundle = build(h)
        with no_automine(c):
            await bundle.matcher.run_once()
            await bundle.relayer.run_once()
            c.send_nowait(c.exchange.functions.cancel(order_struct(body)), seller.key, tip_gwei=50)
            c.mine()
        await bundle.listener.run_once()  # OrderCancelled applied FIRST
        (st,) = await settlements(h)
        assert st.status == SettlementStatus.FAILED and st.failure_reason.startswith("cancel_race")
        await bundle.relayer.run_once()  # sees a failed settlement: nothing to do, no retry
        assert (await settlements(h))[0].attempts == 0
        assert (await order_row(h, buy)).status == OrderStatus.OPEN


# ------------------------------------------------------------------- quote freshness
async def test_a_quote_without_enough_validity_left_is_never_signed(
    make_settings: SettingsFactory, chain: dict[str, str], web3_sync: Web3
) -> None:
    async with stack(make_settings, chain, web3_sync) as (h, c):
        await pair(h, c)
        bundle = build(h)
        await bundle.matcher.run_once()
        # maxStaleness is 300 s and the margin 60 s: a 250 s old quote has < 60 s of life left
        await post_quote(h, c, QUOTE, age=250)
        nonce = relayer_nonce(h, c)
        cycle = await bundle.relayer.run_once()
        assert cycle.deferred == 1 and relayer_nonce(h, c) == nonce
        assert (await settlements(h))[0].status == SettlementStatus.MATCHED
        # the matcher also refuses to match on it
        r = await bundle.matcher.run_once()
        assert r.idle_reason == "stale_quote"
        await post_quote(h, c, QUOTE)  # a fresh one arrives: now it goes through
        await settle_all(bundle, c)
        assert (await settlements(h))[0].status == SettlementStatus.CONFIRMED


async def test_a_stuck_settle_is_replaced_at_the_same_nonce_with_a_fresh_quote(
    make_settings: SettingsFactory, chain: dict[str, str], web3_sync: Web3
) -> None:
    async with stack(make_settings, chain, web3_sync, settle_bump_after_seconds=0.05) as (h, c):
        seller, buyer, sell, buy = await pair(h, c)
        bundle = build(h)
        with no_automine(c):
            await bundle.matcher.run_once()
            await bundle.relayer.run_once()
            (st,) = await settlements(h)
            first = (st.tx_hash, st.tx_nonce, st.quote_timestamp, int(st.quote_price))
            await post_quote(h, c, 152_000)  # the engine publishes a newer quote; tx still pending
            await asyncio.sleep(0.1)
            cycle = await bundle.relayer.run_once()
            assert cycle.bumped == 1
            (st,) = await settlements(h)
            assert st.tx_nonce == first[1] and len(st.tx_hashes) == 2  # same nonce, new tx
            assert st.tx_hash != first[0]
            assert st.quote_timestamp >= first[2] and int(st.quote_price) == 152_000  # fresh quote
            c.mine()
        await bundle.listener.run_once()
        (st,) = await settlements(h)
        (tr,) = await trades(h)
        assert st.status == SettlementStatus.CONFIRMED and tr.tx_hash in st.tx_hashes
        assert int(tr.exec_price) == 152_000  # the price was re-derived from the fresh quote
        # exactly one fill happened on-chain
        assert c.exchange.functions.filled(sell).call() == 1_000
        assert c.balance_watt(buyer.address) == 1_000


async def test_a_quote_that_goes_stale_while_the_tx_waits_is_requoted_not_failed(
    make_settings: SettingsFactory, chain: dict[str, str], web3_sync: Web3
) -> None:
    async with stack(make_settings, chain, web3_sync) as (h, c):
        seller, buyer, sell, buy = await pair(h, c)
        bundle = build(h)
        with no_automine(c):
            await bundle.matcher.run_once()
            await bundle.relayer.run_once()
            (st,) = await settlements(h)
            first_hash = st.tx_hash
            c.increase_time(400, mine=False)  # the pending tx is mined 400 s later: quote is stale
            c.mine()
        assert c.receipt_status(first_hash) == 0  # type: ignore[arg-type]  # StaleQuote on-chain

        await bundle.relayer.run_once()
        (st,) = await settlements(h)
        assert st.status == SettlementStatus.MATCHED and st.attempts == 1  # re-queued, not failed
        assert st.failure_reason.startswith("retry:StaleQuote")
        assert (await order_row(h, buy)).status == OrderStatus.OPEN  # nobody lost their order
        await post_quote(h, c, QUOTE)  # the engine keeps quoting
        await asyncio.sleep(0.3)  # retry backoff
        await settle_all(bundle, c)
        (st,) = await settlements(h)
        assert st.status == SettlementStatus.CONFIRMED and len(await trades(h)) == 1
        assert c.balance_watt(buyer.address) == 1_000


# -------------------------------------------------------------------- release backoff
async def test_a_price_move_that_makes_a_pair_unsettleable_does_not_cause_a_rematch_loop(
    make_settings: SettingsFactory, chain: dict[str, str], web3_sync: Web3
) -> None:
    async with stack(make_settings, chain, web3_sync) as (h, c):
        seller, buyer, sell, buy = await pair(h, c)
        bundle = build(h)
        await bundle.matcher.run_once()  # matched at 150k
        # the market moves: 200k is outside [140k, 160k] AND its band, so no price fits any more
        await post_quote(h, c, 200_000)
        nonce = relayer_nonce(h, c)
        cycle = await bundle.relayer.run_once()
        assert cycle.released == 1 and relayer_nonce(h, c) == nonce
        (st,) = await settlements(h)
        assert st.status == SettlementStatus.FAILED and st.failure_reason == "released:price_band"
        (cd,) = await rows(h, PairCooldown)
        assert (cd.failures, cd.last_reason) == (1, "price_band")
        assert (await order_row(h, sell)).status == OrderStatus.OPEN  # both back on the book
        assert int((await order_row(h, buy)).reserved_wh) == 0

        # twenty more cycles with the unfeasible price: no new settlements, no gas
        for _ in range(20):
            await drive(bundle)
        assert len(await settlements(h)) == 1

        # the price comes back, but the pair is on cooldown: still no re-match
        await post_quote(h, c, QUOTE)
        cooled = 0
        for _ in range(20):
            await drive(bundle)
            cooled += bundle.matcher.last_result.skipped["cooldown"]
        assert len(await settlements(h)) == 1
        assert cooled == 20  # every cycle saw the pair and skipped it because of the cooldown

        # cooldown over => matched again; a second release DOUBLES the backoff
        async with h.app.state.sessionmaker() as s, s.begin():
            row = await s.get(PairCooldown, (cd.buy_hash, cd.sell_hash))
            assert row is not None
            row.until = row.until.replace(year=2000)
        await bundle.matcher.run_once()
        assert len(await settlements(h)) == 2
        await post_quote(h, c, 200_000)
        await bundle.relayer.run_once()
        (cd2,) = await rows(h, PairCooldown)
        assert cd2.failures == 2
        assert (cd2.until - cd.until).total_seconds() > 25  # ~60 s now instead of ~30 s
        assert len(await settlements(h)) == 2

        # finally the market is calm and the cooldown has lapsed: the trade happens, cooldown is cleared
        async with h.app.state.sessionmaker() as s, s.begin():
            row = await s.get(PairCooldown, (cd.buy_hash, cd.sell_hash))
            assert row is not None
            row.until = row.until.replace(year=2000)
        await post_quote(h, c, QUOTE)
        await settle_all(bundle, c)
        assert (await settlements(h))[-1].status == SettlementStatus.CONFIRMED
        assert await rows(h, PairCooldown) == []
