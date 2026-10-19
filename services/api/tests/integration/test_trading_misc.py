"""Full journey through the embedded workers, Redis event streams, admin endpoints and the
pricing-engine feed."""

import asyncio
import json
from typing import Any

import pytest
import respx
from web3 import Web3

from app.models import OrderStatus
from app.pricing import repo
from tests.api.pricing_helpers import FakeClock, make_service, mock_weather, owner_with_meter
from tests.conftest import Harness, SettingsFactory
from tests.integration.test_trading_settlement import pair, stack
from tests.trading_kit import (
    MID,
    Chain,
    build,
    new_trader,
    order_row,
    post_quote,
    seed_market,
    settle_all,
    settlements,
    trading_settings,
    wait_until,
)

pytestmark = pytest.mark.integration
QUOTE = 150_000


# ---------------------------------------------------------- the whole thing, as deployed
async def test_embedded_workers_run_the_full_journey_over_http_only(
    make_settings: SettingsFactory, chain: dict[str, str], web3_sync: Web3
) -> None:
    """The lifespan starts matcher/relayer/listener/sweeper by itself; the test only uses the REST
    API (plus the wallet's own approvals)."""
    settings = trading_settings(
        make_settings, chain, trading_enabled=True, match_poll_ms=100, relayer_poll_seconds=0.1,
        listener_poll_seconds=0.2, admin_addresses="",
    )  # fmt: skip
    async with Harness(settings) as h:
        c = Chain(h, web3_sync)
        await seed_market(h)
        await post_quote(h, c, QUOTE)
        seller = await new_trader(h, c, watt=4_000)
        buyer = await new_trader(h, c, usdc=10_000_000)
        sell = await seller.place_ok("sell", 2_500, 140_000)
        buy = await buyer.place_ok("buy", 2_500, 160_000)

        async def filled() -> bool:
            return bool((await seller.order(sell))["status"] == "filled")

        await wait_until(filled)
        assert (await buyer.order(buy))["status"] == "filled"
        mine = (await buyer.get("/api/v1/trades")).json()
        assert [(t["status"], t["fill_wh"], t["exec_price"]) for t in mine] == [
            ("confirmed", "2500", "150000")
        ]
        assert mine[0]["tx_hash"] and mine[0]["block_number"]
        tape = (await h.client.get(f"/api/v1/markets/{MID}/trades")).json()
        assert [t["fill_wh"] for t in tape] == ["2500"]
        assert c.balance_watt(buyer.address) == 2_500
        assert c.exchange.functions.filled(sell).call() == 2_500
        # all four workers are alive and led by this instance
        trading = h.app.state.trading
        assert all(loop.leader for loop in trading.loops.values())


async def test_admin_state_and_pause_switch(
    make_settings: SettingsFactory, chain: dict[str, str], web3_sync: Web3
) -> None:
    from eth_account import Account

    admin_key = "0x" + "77" * 32
    admin_addr = Account.from_key(admin_key).address
    async with Harness(
        trading_settings(make_settings, chain, admin_addresses=admin_addr.lower())
    ) as h:
        c = Chain(h, web3_sync)
        await seed_market(h)
        await post_quote(h, c, QUOTE)
        from tests.helpers import login

        admin = await login(h, admin_key)
        normal = await new_trader(h, c, watt=1_000)
        assert (await normal.get("/api/v1/admin/trading/state")).status_code == 403
        r = await admin.client.get("/api/v1/admin/trading/state", headers=admin.headers)
        assert r.status_code == 200
        body = r.json()
        assert body["matching_paused"] is False and body["open_orders"] == 0
        assert body["listener"]["cursor"] is not None

        _seller, _buyer, _sell, _buy = await pair(h, c)
        b = build(h)
        pr = await admin.client.post("/api/v1/admin/trading/pause", headers=admin.headers)
        assert pr.json()["matching_paused"] is True
        res = await b.matcher.run_once()
        assert res.idle_reason == "paused_by_admin" and await settlements(h) == []
        rr = await admin.client.post("/api/v1/admin/trading/resume", headers=admin.headers)
        assert rr.json()["matching_paused"] is False
        await settle_all(b, c)
        st = (await admin.client.get("/api/v1/admin/trading/state", headers=admin.headers)).json()
        assert st["settlements"] == {"confirmed": 1}


# ----------------------------------------------------------------------- redis streams
async def test_events_are_published_after_commit_in_the_shared_envelope(
    make_settings: SettingsFactory, chain: dict[str, str], web3_sync: Web3
) -> None:
    async with stack(make_settings, chain, web3_sync) as (h, c):
        redis = h.app.state.redis
        seller = await new_trader(h, c, watt=10_000)
        buyer = await new_trader(h, c, usdc=50_000_000)
        ps = redis.pubsub()
        await ps.subscribe(
            f"orderbook:{MID}",
            f"trades:{MID}",
            f"user:{seller.address.lower()}",
            f"user:{buyer.address.lower()}",
        )
        await asyncio.sleep(0.1)
        await seller.place_ok("sell", 1_000, 140_000)
        await buyer.place_ok("buy", 1_000, 160_000)
        await settle_all(build(h), c)

        seen: list[dict[str, Any]] = []
        for _ in range(200):
            m = await ps.get_message(ignore_subscribe_messages=True, timeout=0.05)
            if m is None:
                if seen and any(e["type"] == "trade" for e in seen):
                    break
                continue
            seen.append(json.loads(m["data"]))
        await ps.aclose()
        assert {"orderbook", "trade", "order", "fill"} <= {e["type"] for e in seen}
        for e in seen:
            assert set(e) == {"type", "channel", "epoch", "seq", "ts", "data"}
        trade = next(e for e in seen if e["type"] == "trade")
        assert trade["channel"] == f"trades:{MID}" and trade["data"]["status"] == "confirmed"
        # per-channel sequence numbers rise without gaps (clients detect holes with them)
        for channel in {e["channel"] for e in seen}:
            seqs = [e["seq"] for e in seen if e["channel"] == channel]
            assert seqs == list(range(seqs[0], seqs[0] + len(seqs)))
        assert not any("signature" in json.dumps(e["data"]) for e in seen)  # never leak signatures


async def test_a_rolled_back_cycle_publishes_nothing(
    make_settings: SettingsFactory, chain: dict[str, str], web3_sync: Web3
) -> None:
    from redis.exceptions import LockError

    async with stack(make_settings, chain, web3_sync) as (h, c):
        await pair(h, c)
        redis = h.app.state.redis
        ps = redis.pubsub()
        await ps.subscribe(f"trades:{MID}", f"orderbook:{MID}")
        await asyncio.sleep(0.1)
        while await ps.get_message(ignore_subscribe_messages=True, timeout=0.05):
            pass
        b = build(h)

        async def no() -> bool:
            return False

        b.matcher.still_leader = no
        with pytest.raises(LockError):
            await b.matcher.run_once()
        assert await ps.get_message(ignore_subscribe_messages=True, timeout=0.3) is None
        await ps.aclose()


# --------------------------------------------------------------- feeding the pricing engine
@respx.mock
async def test_the_book_feeds_the_pricing_engine(
    make_settings: SettingsFactory, chain: dict[str, str], web3_sync: Web3
) -> None:
    async with stack(make_settings, chain, web3_sync, pricing_inventory_refresh_seconds=1) as (
        h,
        c,
    ):
        clock = FakeClock()
        mock_weather(clock)
        await owner_with_meter(h)  # supply exists, so the engine has a market to price
        svc = make_service(h, clock)
        await svc.poll_weather_once()

        async def drivers() -> dict[str, Any]:
            clock.advance(15)
            [tick] = await svc.tick_all()
            return tick.drivers

        d = await drivers()
        assert d["supply"]["inventory_source"] != "open_sells" and d["demand"]["open_bid_wh"] == 0

        seller = await new_trader(h, c, watt=10_000)
        buyer = await new_trader(h, c, usdc=50_000_000)
        sell = await seller.place_ok("sell", 2_000, 140_000)
        bid = await buyer.place_ok("buy", 1_500, 100_000)  # below the ask: rests on the book
        d = await drivers()
        assert (
            d["supply"]["inventory_source"] == "open_sells" and d["supply"]["inventory_wh"] == 2_000
        )
        assert d["demand"]["open_bid_wh"] == 1_500 and d["demand"]["recent_fill_wh"] == 0

        # a soft-cancelled order only counts what is still in flight
        assert (await buyer.delete(bid)).status_code == 200
        d = await drivers()
        assert d["demand"]["open_bid_wh"] == 0

        # a matched-but-unconfirmed trade is not fill velocity; a confirmed one is
        crossing = await buyer.place_ok("buy", 1_000, 160_000)
        b = build(h)
        await post_quote(h, c, QUOTE)  # (the engine's own ticks overwrite the Redis quote)
        await b.matcher.run_once()
        d = await drivers()
        assert d["demand"]["recent_fill_wh"] == 0
        await post_quote(h, c, QUOTE)
        await settle_all(b, c)
        d = await drivers()
        assert d["demand"]["recent_fill_wh"] == 1_000 and d["supply"]["inventory_wh"] == 1_000
        assert (await order_row(h, crossing)).status == OrderStatus.FILLED

        # an unfunded order drops out of the engine's inputs on the next tick
        c.send(c.watt.functions.approve(c.exchange.address, 0), seller.key)
        assert await b.sweeper.run_once(full=True) == {sell}
        d = await drivers()
        assert (
            d["supply"]["inventory_wh"] != 1_000 or d["supply"]["inventory_source"] != "open_sells"
        )
        async with h.app.state.sessionmaker() as s:
            assert await repo.open_order_wh(s, MID, (await order_row(h, sell)).side, 0) == 0


# ------------------------------------------------------------------ EIP-1271 contract wallets
async def test_a_contract_wallet_maker_is_verified_via_erc1271_and_settles(
    make_settings: SettingsFactory, chain: dict[str, str], web3_sync: Web3
) -> None:
    """The exchange accepts EIP-1271 makers (SignatureChecker); the API must too, or it would
    reject orders the contract would honour. Auth is by SIWE (EOA), so this drives the
    service layer directly with the wallet as maker."""
    import secrets
    import subprocess
    import uuid
    from decimal import Decimal

    from eth_account import Account

    from app.chain import orders as eip712
    from app.matching import service
    from app.matching.events import Outbox
    from app.models import User
    from app.schemas.trading import OrderIn
    from tests.conftest import CONTRACTS, KEY_ALICE, _tool

    forge = _tool("forge")
    assert forge
    await asyncio.to_thread(
        subprocess.run,
        [forge, "build"],
        cwd=CONTRACTS,
        check=True,
        capture_output=True,
        timeout=300,
    )
    artifact = json.loads(
        (CONTRACTS / "out" / "MockSmartWallet.sol" / "MockSmartWallet.json").read_text()
    )

    async with stack(make_settings, chain, web3_sync) as (h, c):
        signer = Account.from_key(KEY_ALICE)
        c.eth(signer.address)
        factory = web3_sync.eth.contract(
            abi=artifact["abi"], bytecode=artifact["bytecode"]["object"]
        )
        tx = factory.constructor(signer.address).build_transaction(
            {"from": signer.address, "nonce": web3_sync.eth.get_transaction_count(signer.address)}
        )
        rcpt = web3_sync.eth.wait_for_transaction_receipt(
            web3_sync.eth.send_raw_transaction(signer.sign_transaction(tx).raw_transaction)
        )
        wallet_addr = rcpt["contractAddress"]
        assert wallet_addr is not None
        wallet = web3_sync.eth.contract(address=wallet_addr, abi=artifact["abi"])
        c.fund_watt(wallet_addr, 5_000)
        c.send(wallet.functions.approve(c.watt.address, c.exchange.address, 2**255), KEY_ALICE)
        buyer = await new_trader(h, c, usdc=50_000_000)

        # a user row for the wallet (it cannot SIWE-sign; the order signature is what matters)
        async with h.app.state.sessionmaker() as s, s.begin():
            user = User(id=uuid.uuid4(), address=wallet_addr.lower())
            s.add(user)
        cx = h.app.state.chain.client
        data = eip712.OrderData(
            wallet_addr.lower(),
            MID,
            1,
            1_000,
            140_000,
            c.block_ts() + 3600,
            1,
            "0x" + secrets.token_hex(32),
        )
        digest = eip712.order_digest(h.settings.chain_id, cx.exchange_address, data)
        sig = "0x" + Account._sign_hash(digest, KEY_ALICE).signature.hex()

        def body(signature: str) -> OrderIn:
            return OrderIn.model_validate(
                {
                    "maker": wallet_addr, "marketId": MID, "side": 1, "amountWh": "1000",
                    "limitPrice": "140000", "expiry": str(data.expiry), "nonce": "1", "salt": data.salt,
                    "signature": signature,
                }
            )  # fmt: skip

        async def post(signature: str) -> Any:
            out = Outbox()
            async with h.app.state.sessionmaker() as s:
                u = await s.get(User, user.id)
                assert u is not None
                got = await service.accept_order(
                    settings=h.settings, session=s, redis=h.app.state.redis, client=cx, user=u,
                    body=body(signature), out=out,
                )  # fmt: skip
                await s.commit()
                return got

        from app.core.errors import AppError

        bad = "0x" + "11" * 32 + "22" * 32 + "1b"
        with pytest.raises(AppError) as exc:
            await post(bad)  # wrong signature: ERC-1271 says no
        assert exc.value.code == "bad_signature"
        accepted = await post(sig)  # right signature: ERC-1271 says yes
        assert accepted.created and int(accepted.order.amount_wh) == 1_000

        await buyer.place_ok("buy", 1_000, 160_000)
        await settle_all(build(h), c)
        assert (
            c.balance_watt(buyer.address) == 1_000
        )  # the exchange honoured the 1271 signature too
        assert c.balance_watt(wallet_addr) == 4_000
        _ = Decimal


async def test_nonce_floor_raised_on_chain_cancels_only_older_orders(
    make_settings: SettingsFactory, chain: dict[str, str], web3_sync: Web3
) -> None:
    async with stack(make_settings, chain, web3_sync) as (h, c):
        t = await new_trader(h, c, watt=10_000)
        old = await t.place_ok("sell", 1_000, 140_000, nonce=1)
        also_old = await t.place_ok("sell", 1_000, 141_000, nonce=2)
        newer = await t.place_ok("sell", 1_000, 142_000, nonce=3)
        c.send(c.exchange.functions.cancelUpTo(3), t.key)
        b = build(h)
        await b.listener.run_once()
        for hash_ in (old, also_old):
            o = await order_row(h, hash_)
            assert (o.status, o.cancel_type, o.cancel_reason) == (
                OrderStatus.CANCELLED,
                "onchain",
                "nonce_floor",
            )
        assert (await order_row(h, newer)).status == OrderStatus.OPEN
