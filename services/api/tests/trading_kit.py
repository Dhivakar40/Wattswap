"""Test kit for the trading engine on Anvil: wallets, funding, signed orders, driven workers.

TEST ONLY. Funding uses `anvil_setStorageAt` on the token balance mappings (slot 0 of both
EnergyToken and MockUSDC) instead of running the meter->mint pipeline for every wallet; the mint
pipeline itself is covered by the Phase 2 end-to-end tests. Everything the trading engine does with
those balances (approvals, orders, matching, settlement on the real exchange) is real.
"""

import asyncio
import json
import secrets
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

import httpx
from eth_abi import encode
from eth_account import Account
from eth_utils import keccak
from hexbytes import HexBytes
from meter_sim.provision import Session
from sqlalchemy import select
from web3 import Web3

from app.chain import orders as eip712
from app.models import IndexerBlock, IndexerState, Order, Settlement, Trade
from app.pricing import publish as price_publish
from app.pricing.quotes import PriceQuote, sign_quote
from app.pricing.repo import DEFAULT_MARKET_ID, ensure_default_market
from app.workers.listener import CURSOR
from app.workers.trading import TradingBundle, build_trading
from tests.conftest import KEY_PRICER, KEY_RELAYER, Harness, SettingsFactory
from tests.helpers import login

MID = DEFAULT_MARKET_ID
MAX_UINT = 2**255

TRADING_SETTINGS: dict[str, Any] = {
    "relayer_private_key": KEY_RELAYER,
    "trading_enabled": False,  # tests drive the workers explicitly
    "listener_confirmations": 1,
    "settle_bump_after_seconds": 3600.0,
    "settle_backoff_base_seconds": 0.05,
    "settle_backoff_max_seconds": 0.2,
    "pair_cooldown_seconds": 30,
    "chain_params_ttl_seconds": 0.0,
}


def trading_settings(make_settings: SettingsFactory, chain: dict[str, str], **kw: Any) -> Any:
    return make_settings(usdc_address=chain["MockUSDC"], **{**TRADING_SETTINGS, **kw})


class Chain:
    """Synchronous web3 handles used to move funds and send maker transactions."""

    def __init__(self, h: Harness, w3: Web3) -> None:
        self.w3 = w3
        c = h.app.state.chain.client
        load = h.settings.load_abi
        self.exchange = w3.eth.contract(address=c.exchange_address, abi=load("WattSwapExchange"))
        self.watt = w3.eth.contract(address=c.token_address, abi=load("EnergyToken"))
        self.usdc = w3.eth.contract(address=c.usdc_address, abi=load("MockUSDC"))
        self.oracle = w3.eth.contract(address=c.oracle_address, abi=load("PriceOracle"))
        self.decoder = c.decoder

    def rpc(self, method: str, params: list[Any]) -> Any:
        resp = self.w3.provider.make_request(method, params)  # type: ignore[arg-type]
        assert "error" not in resp, resp
        return resp.get("result")

    def eth(self, address: str, amount: int = 10**18) -> None:
        self.rpc("anvil_setBalance", [address, hex(amount)])

    def _set_balance(self, token: str, owner: str, amount: int) -> None:
        slot = keccak(encode(["address", "uint256"], [owner, 0]))
        self.rpc(
            "anvil_setStorageAt",
            [token, "0x" + slot.hex(), "0x" + amount.to_bytes(32, "big").hex()],
        )

    def fund_watt(self, address: str, wh: int) -> None:
        self._set_balance(self.watt.address, address, wh)

    def fund_usdc(self, address: str, micro: int) -> None:
        self._set_balance(self.usdc.address, address, micro)

    def send(self, fn: Any, key: str, **tx: Any) -> Any:
        acct = Account.from_key(key)
        self.eth(acct.address)
        params = {
            "from": acct.address,
            "nonce": self.w3.eth.get_transaction_count(acct.address),
            **tx,
        }
        built = fn.build_transaction(params)
        h = self.w3.eth.send_raw_transaction(acct.sign_transaction(built).raw_transaction)
        rcpt = self.w3.eth.wait_for_transaction_receipt(h, timeout=30)
        assert rcpt["status"] == 1, "transaction reverted"
        return rcpt

    def send_nowait(self, fn: Any, key: str, *, tip_gwei: int = 0) -> str:
        """Broadcast without waiting for a receipt (for automine-off races). A high `tip_gwei`
        makes Anvil order the tx before lower-tip ones in the same block."""
        acct = Account.from_key(key)
        self.eth(acct.address)
        params: dict[str, Any] = {
            "from": acct.address,
            "nonce": self.w3.eth.get_transaction_count(acct.address, "pending"),
        }
        if tip_gwei:
            params["maxPriorityFeePerGas"] = Web3.to_wei(tip_gwei, "gwei")
            params["maxFeePerGas"] = Web3.to_wei(tip_gwei + 100, "gwei")
        built = fn.build_transaction(params)
        return (
            "0x"
            + bytes(
                self.w3.eth.send_raw_transaction(acct.sign_transaction(built).raw_transaction)
            ).hex()
        )

    def receipt_status(self, tx_hash: str) -> int:
        return int(self.w3.eth.get_transaction_receipt(tx_hash)["status"])  # type: ignore[arg-type]

    def block_ts(self) -> int:
        return int(self.w3.eth.get_block("latest")["timestamp"])

    def set_automine(self, on: bool) -> None:
        self.rpc("evm_setAutomine", [on])

    def mine(self, n: int = 1) -> None:
        for _ in range(n):
            self.rpc("evm_mine", [])

    def increase_time(self, seconds: int, *, mine: bool = True) -> None:
        """Move chain time forward. `mine=False` leaves pending txs pending (the next block that
        is mined, with whatever is in the mempool, carries the later timestamp)."""
        self.rpc("evm_increaseTime", [seconds])
        if mine:
            self.mine()

    def balance_watt(self, a: str) -> int:
        return int(self.watt.functions.balanceOf(a).call())

    def balance_usdc(self, a: str) -> int:
        return int(self.usdc.functions.balanceOf(a).call())


@dataclass
class Trader:
    key: str
    address: str
    session: Session
    chain: Chain
    h: Harness
    nonce: int = 1

    def body(
        self,
        side: str,
        wh: int,
        price: int,
        *,
        expires_in: int = 3600,
        nonce: int | None = None,
        market: str = MID,
    ) -> dict[str, Any]:
        """A signed order body for POST /orders (side: "buy" | "sell")."""
        data = eip712.OrderData(
            maker=self.address.lower(),
            market_id=market,
            side=0 if side == "buy" else 1,
            amount_wh=wh,
            limit_price=price,
            expiry=self.chain.block_ts() + expires_in,
            nonce=self.nonce if nonce is None else nonce,
            salt="0x" + secrets.token_hex(32),
        )
        client = self.h.app.state.chain.client
        digest = eip712.order_digest(self.h.settings.chain_id, client.exchange_address, data)
        sig = Account._sign_hash(digest, self.key)
        return {
            "maker": self.address,
            "marketId": data.market_id,
            "side": data.side,
            "amountWh": str(data.amount_wh),
            "limitPrice": str(data.limit_price),
            "expiry": str(data.expiry),
            "nonce": str(data.nonce),
            "salt": data.salt,
            "signature": "0x" + sig.signature.hex(),
            "_hash": "0x" + digest.hex(),
        }

    async def place(self, body: dict[str, Any]) -> httpx.Response:
        payload = {k: v for k, v in body.items() if not k.startswith("_")}
        return await self.session.client.post(
            "/api/v1/orders", headers=self.session.headers, json=payload
        )

    async def place_ok(self, side: str, wh: int, price: int, **kw: Any) -> str:
        body = self.body(side, wh, price, **kw)
        r = await self.place(body)
        assert r.status_code in (200, 201), r.text
        return str(r.json()["order_hash"])

    async def get(self, path: str) -> httpx.Response:
        return await self.session.client.get(path, headers=self.session.headers)

    async def delete(self, order_hash: str) -> httpx.Response:
        return await self.session.client.delete(
            f"/api/v1/orders/{order_hash}", headers=self.session.headers
        )

    async def order(self, order_hash: str) -> dict[str, Any]:
        r = await self.get(f"/api/v1/orders/{order_hash}")
        assert r.status_code == 200, r.text
        body: dict[str, Any] = r.json()
        return body

    def approve(self, watt: bool = True, usdc: bool = True, amount: int = MAX_UINT) -> None:
        ex = self.chain.exchange.address
        if watt:
            self.chain.send(self.chain.watt.functions.approve(ex, amount), self.key)
        if usdc:
            self.chain.send(self.chain.usdc.functions.approve(ex, amount), self.key)


async def new_trader(
    h: Harness, chain: Chain, *, watt: int = 0, usdc: int = 0, approve: bool = True
) -> Trader:
    key = "0x" + secrets.token_hex(32)
    address = Account.from_key(key).address
    chain.eth(address)
    if watt:
        chain.fund_watt(address, watt)
    if usdc:
        chain.fund_usdc(address, usdc)
    session = await login(h, key)
    t = Trader(key=key, address=address, session=session, chain=chain, h=h)
    if approve:
        t.approve()
    return t


async def seed_market(h: Harness) -> None:
    """Create the default market and start the listener at the current head (the shared Anvil
    carries events from earlier tests that this test must not replay)."""
    async with h.app.state.sessionmaker() as s:
        await ensure_default_market(s)  # commits itself
    w3 = h.app.state.chain.client.w3
    head = int(await w3.eth.block_number)
    block = await w3.eth.get_block(head)
    async with h.app.state.sessionmaker() as s, s.begin():
        s.add(IndexerState(name=CURSOR, last_block=head))
        s.add(IndexerBlock(block_number=head, block_hash="0x" + bytes(block["hash"]).hex()))


async def post_quote(h: Harness, chain: Chain, price: int, *, age: int = 0) -> PriceQuote:
    """Publish a signed engine quote exactly where the pricing engine does (Redis)."""
    ts = chain.block_ts() - age
    q = PriceQuote(market_id=MID, price=price, timestamp=ts)
    signed = sign_quote(
        KEY_PRICER, h.settings.chain_id, h.app.state.chain.client.exchange_address, q
    )
    await h.app.state.redis.set(price_publish.quote_key(MID), json.dumps(signed.to_json()))
    return q


def build(h: Harness, suffix: str = "", *, leader_checks: bool = False) -> TradingBundle:
    """A trading bundle driven by plain `run_once()` calls (no leader loops running)."""
    b = build_trading(
        h.settings,
        h.app.state.sessionmaker,
        h.app.state.redis,
        h.app.state.chain.client,
        suffix=suffix,
    )
    if not leader_checks:
        b.matcher.still_leader = None
    return b


async def drive(b: TradingBundle, rounds: int = 1) -> None:
    """One matcher -> relayer -> listener pass, `rounds` times (plain calls: no leader locks)."""
    for _ in range(rounds):
        await b.matcher.run_once()
        await b.relayer.run_once()
        await b.listener.run_once()


async def settle_all(b: TradingBundle, chain: Chain, *, rounds: int = 6) -> None:
    """Drive until nothing is matched/submitted any more (bounded)."""
    for _ in range(rounds):
        await drive(b)
        chain.mine()
        await asyncio.sleep(0.05)


async def rows(h: Harness, model: Any, **where: Any) -> list[Any]:
    async with h.app.state.sessionmaker() as s:
        q = select(model)
        for k, v in where.items():
            q = q.where(getattr(model, k) == v)
        return list((await s.scalars(q)).all())


async def order_row(h: Harness, order_hash: str) -> Order:
    (o,) = await rows(h, Order, order_hash=order_hash)
    assert isinstance(o, Order)
    return o


async def settlements(h: Harness) -> list[Settlement]:
    return await rows(h, Settlement)


async def trades(h: Harness) -> list[Trade]:
    return await rows(h, Trade)


def order_struct(body: dict[str, Any]) -> tuple[Any, ...]:
    """The `Order` tuple for cancel(order) from a body built by `Trader.body`."""
    return (
        Web3.to_checksum_address(body["maker"]),
        bytes.fromhex(body["marketId"][2:]),
        body["side"],
        int(body["amountWh"]),
        int(body["limitPrice"]),
        int(body["expiry"]),
        int(body["nonce"]),
        bytes.fromhex(body["salt"][2:]),
    )


async def wait_until(predicate: Callable[[], Awaitable[bool]], within: float = 40.0) -> None:
    """Poll an async condition until it holds (background workers are running)."""
    deadline = asyncio.get_event_loop().time() + within
    while asyncio.get_event_loop().time() < deadline:
        if await predicate():
            return
        await asyncio.sleep(0.2)
    raise AssertionError("condition not reached in time")


def now() -> float:
    return time.time()


_ = HexBytes
