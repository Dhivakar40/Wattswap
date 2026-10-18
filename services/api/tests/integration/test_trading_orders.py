"""Order acceptance, over-commitment, rate limiting, minimum size and the soft/hard cancel API."""

from typing import Any

import httpx
import pytest
from eth_account import Account
from web3 import Web3

from app.chain.orders import HALF_N, SECP256K1_N
from app.models import OrderStatus
from tests.conftest import Harness, SettingsFactory
from tests.trading_kit import (
    MID,
    Chain,
    Trader,
    build,
    new_trader,
    order_row,
    post_quote,
    seed_market,
    settle_all,
    settlements,
    trading_settings,
)

pytestmark = pytest.mark.integration
QUOTE = 150_000


async def env(
    make_settings: SettingsFactory, chain: dict[str, str], w3: Web3, **kw: Any
) -> tuple[Harness, Chain]:
    h = Harness(trading_settings(make_settings, chain, **kw))
    await h.__aenter__()
    c = Chain(h, w3)
    await seed_market(h)
    await post_quote(h, c, QUOTE)
    return h, c


def code(r: Any) -> str:
    return str(r.json()["error"]["code"])


# ------------------------------------------------------------------------------ rejections
async def test_signature_and_format_rejections(
    make_settings: SettingsFactory, chain: dict[str, str], web3_sync: Web3
) -> None:
    h, c = await env(make_settings, chain, web3_sync)
    try:
        seller = await new_trader(h, c, watt=10_000)
        other = await new_trader(h, c, watt=10_000)

        # wrong signer
        body = seller.body("sell", 1_000, 140_000)
        forged = other.body("sell", 1_000, 140_000)
        body["signature"] = forged["signature"]
        assert code(await seller.place(body)) == "bad_signature"

        # high-s (malleated) signature: valid ECDSA, rejected by OpenZeppelin on-chain => rejected here
        good = seller.body("sell", 1_000, 140_000)
        raw = bytes.fromhex(good["signature"][2:])
        r, s, v = raw[:32], int.from_bytes(raw[32:64], "big"), raw[64]
        malleated = r + (SECP256K1_N - s).to_bytes(32, "big") + bytes([55 - v])
        assert (SECP256K1_N - s) > HALF_N
        good["signature"] = "0x" + malleated.hex()
        assert code(await seller.place(good)) == "bad_signature"

        # 64-byte compact signature
        compact = seller.body("sell", 1_000, 140_000)
        compact["signature"] = compact["signature"][:130]
        assert code(await seller.place(compact)) == "bad_signature"

        # somebody else's order
        theirs = other.body("sell", 1_000, 140_000)
        assert code(await seller.place(theirs)) == "maker_mismatch"

        # unauthenticated
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=h.app), base_url="https://localhost"
        ) as anon:  # h.client keeps the cookie of the last login
            r = await anon.post(
                "/api/v1/orders", json={k: v for k, v in good.items() if k[0] != "_"}
            )
        assert r.status_code == 401

        # an order signed for ANOTHER chain/contract has a different digest => bad signature
        replay = seller.body("sell", 1_000, 140_000)
        replay["amountWh"] = "1001"  # any field change invalidates the signature
        assert code(await seller.place(replay)) == "bad_signature"
    finally:
        await h.__aexit__(None, None, None)


async def test_bounds_minimum_size_expiry_and_nonce(
    make_settings: SettingsFactory, chain: dict[str, str], web3_sync: Web3
) -> None:
    h, c = await env(make_settings, chain, web3_sync)
    try:
        t = await new_trader(h, c, watt=100_000)
        assert code(await t.place(t.body("sell", 99, 140_000))) == "order_too_small"
        assert code(await t.place(t.body("sell", 1_000, 10))) == "bad_price"
        assert code(await t.place(t.body("sell", 1_000, 10**12))) == "bad_price"
        assert code(await t.place(t.body("sell", 1_000, 140_000, expires_in=30))) == "order_expired"
        assert code(await t.place(t.body("sell", 1_000, 140_000, expires_in=10**8))) == "bad_expiry"
        assert (
            code(await t.place(t.body("sell", 1_000, 140_000, market="0x" + "ab" * 32)))
            == "market_disabled"
        )
        # nonce below the on-chain floor
        c.send(c.exchange.functions.cancelUpTo(10), t.key)
        assert code(await t.place(t.body("sell", 1_000, 140_000, nonce=5))) == "nonce_too_low"
        assert (await t.place(t.body("sell", 1_000, 140_000, nonce=10))).status_code == 201
        # exactly the minimum is fine
        assert (await t.place(t.body("sell", 100, 140_000, nonce=10))).status_code == 201
    finally:
        await h.__aexit__(None, None, None)


async def test_posting_the_same_signed_order_twice_is_idempotent(
    make_settings: SettingsFactory, chain: dict[str, str], web3_sync: Web3
) -> None:
    h, c = await env(make_settings, chain, web3_sync)
    try:
        t = await new_trader(h, c, watt=5_000)
        body = t.body("sell", 1_000, 140_000)
        first, second = await t.place(body), await t.place(body)
        assert (first.status_code, second.status_code) == (201, 200)
        assert first.json()["order_hash"] == second.json()["order_hash"] == body["_hash"]
        assert len((await t.get("/api/v1/orders")).json()) == 1
        # our digest is the exchange's digest
        order = (
            Web3.to_checksum_address(t.address), bytes.fromhex(MID[2:]), 1, 1_000, 140_000,
            int(body["expiry"]), t.nonce, bytes.fromhex(body["salt"][2:]),
        )  # fmt: skip
        assert "0x" + c.exchange.functions.hashOrder(order).call().hex() == body["_hash"]
    finally:
        await h.__aexit__(None, None, None)


# ------------------------------------------------------------------------ over-commitment
async def test_a_maker_cannot_commit_more_than_balance_across_many_orders(
    make_settings: SettingsFactory, chain: dict[str, str], web3_sync: Web3
) -> None:
    h, c = await env(make_settings, chain, web3_sync)
    try:
        t = await new_trader(h, c, watt=2_500)
        assert (await t.place(t.body("sell", 1_000, 140_000))).status_code == 201
        assert (await t.place(t.body("sell", 1_000, 141_000))).status_code == 201
        r = await t.place(t.body("sell", 600, 142_000))  # 2_600 > 2_500
        assert r.status_code == 422 and code(r) == "insufficient_balance"
        d = r.json()["error"]["details"]
        assert (d["required"], d["already_committed"], d["balance"]) == (600, 2_000, 2_500)
        assert (await t.place(t.body("sell", 500, 142_000))).status_code == 201  # exactly fits
        assert code(await t.place(t.body("sell", 100, 142_000))) == "insufficient_balance"
    finally:
        await h.__aexit__(None, None, None)


async def test_allowance_is_checked_separately_from_balance(
    make_settings: SettingsFactory, chain: dict[str, str], web3_sync: Web3
) -> None:
    h, c = await env(make_settings, chain, web3_sync)
    try:
        t = await new_trader(h, c, watt=10_000, approve=False)
        t.approve(usdc=False, amount=1_500)  # plenty of WATT, small allowance
        assert (await t.place(t.body("sell", 1_000, 140_000))).status_code == 201
        r = await t.place(t.body("sell", 600, 140_000))
        assert code(r) == "insufficient_allowance"
        assert r.json()["error"]["details"]["allowance"] == 1_500
        assert r.json()["error"]["details"]["balance"] == 10_000
    finally:
        await h.__aexit__(None, None, None)


async def test_buy_side_commitment_is_worst_case_cost_at_the_limit_price(
    make_settings: SettingsFactory, chain: dict[str, str], web3_sync: Web3
) -> None:
    h, c = await env(make_settings, chain, web3_sync)
    try:
        # 2 kWh at a 0.16 limit costs at most 320_000; two such orders need 640_000
        t = await new_trader(h, c, usdc=700_000)
        assert (await t.place(t.body("buy", 2_000, 160_000))).status_code == 201
        assert (await t.place(t.body("buy", 2_000, 160_000))).status_code == 201
        r = await t.place(t.body("buy", 1_000, 160_000))  # +160_000 => 800_000 > 700_000
        assert code(r) == "insufficient_balance"
        assert r.json()["error"]["details"]["required"] == 160_000
        assert (await t.place(t.body("buy", 1_000, 60_000))).status_code == 201  # 60_000 fits
    finally:
        await h.__aexit__(None, None, None)


# ------------------------------------------------------------------------------ rate limit
async def test_order_placement_is_rate_limited_per_address_not_per_ip(
    make_settings: SettingsFactory, chain: dict[str, str], web3_sync: Web3
) -> None:
    h, c = await env(make_settings, chain, web3_sync, rate_limit_orders=3)
    try:
        a = await new_trader(h, c, watt=100_000)
        b = await new_trader(h, c, watt=100_000)
        for _ in range(3):
            assert (await a.place(a.body("sell", 1_000, 140_000))).status_code == 201
        r = await a.place(a.body("sell", 1_000, 140_000))
        assert r.status_code == 429 and code(r) == "rate_limited" and "retry-after" in r.headers
        # same IP, different address: its own budget
        assert (await b.place(b.body("sell", 1_000, 140_000))).status_code == 201
    finally:
        await h.__aexit__(None, None, None)


async def test_open_order_cap(
    make_settings: SettingsFactory, chain: dict[str, str], web3_sync: Web3
) -> None:
    h, c = await env(make_settings, chain, web3_sync, max_open_orders_per_maker=2)
    try:
        t = await new_trader(h, c, watt=100_000)
        for _ in range(2):
            assert (await t.place(t.body("sell", 1_000, 140_000))).status_code == 201
        assert code(await t.place(t.body("sell", 1_000, 140_000))) == "too_many_open_orders"
    finally:
        await h.__aexit__(None, None, None)


# ----------------------------------------------------------------------- config + book reads
async def test_order_config_book_and_ownership(
    make_settings: SettingsFactory, chain: dict[str, str], web3_sync: Web3
) -> None:
    h, c = await env(make_settings, chain, web3_sync)
    try:
        cfg = (await h.client.get(f"/api/v1/markets/{MID}/order-config")).json()
        assert cfg["chain_id"] == 31337 and cfg["primary_type"] == "Order"
        assert (
            cfg["eip712_domain"]["verifyingContract"] == h.app.state.chain.client.exchange_address
        )
        assert {f["name"] for f in cfg["cancel_onchain_abi"]} == {"cancel", "cancelUpTo"}
        assert cfg["min_order_wh"] == 100 and cfg["band_bps"] == 500 and cfg["fee_bps"] == 100

        a = await new_trader(h, c, watt=10_000)
        b = await new_trader(h, c, watt=10_000)
        await a.place_ok("sell", 1_000, 150_000)
        await b.place_ok("sell", 2_000, 150_000)
        await a.place_ok("sell", 500, 160_000)
        book = (await h.client.get(f"/api/v1/markets/{MID}/book")).json()
        assert book["asks"] == [
            {"price": "150000", "amount_wh": "3000", "orders": 2},
            {"price": "160000", "amount_wh": "500", "orders": 1},
        ]
        assert "maker" not in str(book)  # aggregated: no addresses
        theirs = (await a.get("/api/v1/orders")).json()[0]["order_hash"]
        assert (await b.get(f"/api/v1/orders/{theirs}")).status_code == 404  # not yours
        assert (await b.delete(theirs)).status_code == 404
        assert (await h.client.get(f"/api/v1/markets/{'0x' + 'cd' * 32}/book")).status_code == 404
    finally:
        await h.__aexit__(None, None, None)


# ---------------------------------------------------------------------- soft / hard cancel
async def test_off_chain_cancel_removes_from_the_book_but_is_not_binding(
    make_settings: SettingsFactory, chain: dict[str, str], web3_sync: Web3
) -> None:
    h, c = await env(make_settings, chain, web3_sync)
    try:
        t = await new_trader(h, c, watt=5_000)
        hash_ = await t.place_ok("sell", 1_000, 140_000)
        r = await t.delete(hash_)
        assert r.status_code == 200
        body = r.json()
        assert body["cancel_type"] == "offchain" and body["binding_onchain"] is False
        assert body["cancel_pending"] is False and body["cancelled_wh"] == "1000"
        assert body["order"]["status"] == "cancelled" and body["order"]["cancel_type"] == "offchain"
        assert body["order"]["cancel_reason"] == "user"
        assert (await t.delete(hash_)).status_code == 200  # idempotent
        book = (await h.client.get(f"/api/v1/markets/{MID}/book")).json()
        assert book["asks"] == []
        # ... and the signature is STILL valid on-chain: the exchange has not been told
        assert c.exchange.functions.cancelled(hash_).call() is False
    finally:
        await h.__aexit__(None, None, None)


async def test_cancel_with_reserved_wh_cancels_only_the_unreserved_remainder(
    make_settings: SettingsFactory, chain: dict[str, str], web3_sync: Web3
) -> None:
    h, c = await env(make_settings, chain, web3_sync)
    try:
        seller = await new_trader(h, c, watt=10_000)
        buyer = await new_trader(h, c, usdc=10_000_000)
        await post_quote(h, c, QUOTE)
        sell = await seller.place_ok("sell", 3_000, 140_000)
        buy = await buyer.place_ok("buy", 1_000, 160_000)
        b = build(h)
        await b.matcher.run_once()  # 1_000 Wh reserved on the sell order, NOT yet submitted
        o = await order_row(h, sell)
        assert (int(o.reserved_wh), int(o.filled_wh)) == (1_000, 0)

        r = await seller.delete(sell)
        body = r.json()
        assert r.status_code == 200 and body["cancel_pending"] is True
        assert body["cancelled_wh"] == "2000" and body["reserved_wh"] == "1000"
        assert body["order"]["status"] == "open" and body["order"]["cancel_pending"] is True
        assert "in-flight" in body["message"]
        # the reserved part still settles ...
        await settle_all(b, c)
        o = await order_row(h, sell)
        assert int(o.filled_wh) == 1_000 and int(o.reserved_wh) == 0
        # ... and the order is then finalized as cancelled (the rest was cancelled)
        assert o.status == OrderStatus.CANCELLED and o.cancel_pending is False
        assert o.cancel_type == "offchain"
        assert c.balance_watt(buyer.address) == 1_000
        assert (await order_row(h, buy)).status == OrderStatus.FILLED
        # a pending-cancel order is not matchable: a new buyer finds nothing to trade with
        await buyer.place_ok("buy", 1_000, 160_000)
        await settle_all(b, c)
        assert c.balance_watt(buyer.address) == 1_000
    finally:
        await h.__aexit__(None, None, None)


async def test_cancel_pending_order_is_cancelled_in_full_when_the_settlement_fails(
    make_settings: SettingsFactory, chain: dict[str, str], web3_sync: Web3
) -> None:
    h, c = await env(make_settings, chain, web3_sync)
    try:
        seller = await new_trader(h, c, watt=10_000)
        buyer = await new_trader(h, c, usdc=10_000_000)
        sell = await seller.place_ok("sell", 1_000, 140_000)
        await buyer.place_ok("buy", 1_000, 160_000)
        b = build(h)
        await b.matcher.run_once()
        assert (await seller.delete(sell)).json()["cancel_pending"] is True
        # the buyer pulls the money before the relayer submits: the settlement fails pre-flight
        c.send(c.usdc.functions.approve(c.exchange.address, 0), buyer.key)
        await settle_all(b, c)
        (st,) = await settlements(h)
        assert st.status.value == "failed"
        o = await order_row(h, sell)
        assert (
            o.status == OrderStatus.CANCELLED and int(o.reserved_wh) == 0 and int(o.filled_wh) == 0
        )
        assert o.cancel_type == "offchain"
    finally:
        await h.__aexit__(None, None, None)


_ = (Trader, Account)
