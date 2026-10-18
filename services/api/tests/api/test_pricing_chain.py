"""The engine's output against the real contracts on Anvil.

* a quote signed in Python is accepted by WattSwapExchange (digest, signer role, staleness, band);
* a real settle() with the engine's integer price moves exactly the amounts the formula says;
* the oracle pusher writes the price and obeys its threshold / heartbeat / backoff rules.

Note on "verifies via PriceOracle": the oracle stores the price and defines staleness and who may
sign; the signature is checked by the exchange, which asks the oracle for `PRICER_ROLE`.
"""

import asyncio
import secrets
import time
from dataclasses import replace
from typing import Any

import pytest
import respx
from eth_account import Account
from eth_account.messages import encode_typed_data
from eth_utils import keccak
from web3 import Web3
from web3.exceptions import ContractCustomError, ContractLogicError

from app.chain.tx import TxSender
from app.pricing import repo
from app.pricing.quotes import PriceQuote, quote_typed_data, sign_quote
from app.pricing.repo import DEFAULT_MARKET_ID
from tests.api.pricing_helpers import FakeClock, make_service, mock_weather, owner_with_meter
from tests.conftest import (
    KEY_DEPLOYER,
    KEY_PRICER,
    KEY_RELAYER,
    Harness,
    SettingsFactory,
)
from tests.helpers import INTERVAL, make_worker, past_start, post_reading, signed_reading

MID = DEFAULT_MARKET_ID
ORDER_TYPES = {
    "EIP712Domain": [
        {"name": "name", "type": "string"},
        {"name": "version", "type": "string"},
        {"name": "chainId", "type": "uint256"},
        {"name": "verifyingContract", "type": "address"},
    ],
    "Order": [
        {"name": "maker", "type": "address"},
        {"name": "marketId", "type": "bytes32"},
        {"name": "side", "type": "uint8"},
        {"name": "amountWh", "type": "uint256"},
        {"name": "limitPrice", "type": "uint256"},
        {"name": "expiry", "type": "uint256"},
        {"name": "nonce", "type": "uint256"},
        {"name": "salt", "type": "bytes32"},
    ],
}


# ---------------------------------------------------------------------------- helpers
class Chain:
    """Sync web3 handles on the deployed contracts, for sending test transactions."""

    def __init__(self, h: Harness, w3: Web3) -> None:
        self.h, self.w3 = h, w3
        client = h.app.state.chain.client
        load = h.settings.load_abi
        self.exchange = w3.eth.contract(
            address=client.exchange_address, abi=load("WattSwapExchange")
        )
        self.oracle = w3.eth.contract(address=client.oracle_address, abi=load("PriceOracle"))
        self.token = w3.eth.contract(address=client.token_address, abi=load("EnergyToken"))
        self.usdc = w3.eth.contract(
            address=self.exchange.functions.usdc().call(), abi=load("MockUSDC")
        )
        self.decoder = client.decoder

    def fund(self, address: str) -> None:
        self.w3.provider.make_request("anvil_setBalance", [address, hex(10**18)])  # type: ignore[arg-type]

    def send(self, fn: Any, key: str) -> Any:
        acct = Account.from_key(key)
        self.fund(acct.address)
        tx = fn.build_transaction(
            {"from": acct.address, "nonce": self.w3.eth.get_transaction_count(acct.address)}
        )
        rcpt = self.w3.eth.wait_for_transaction_receipt(
            self.w3.eth.send_raw_transaction(acct.sign_transaction(tx).raw_transaction)
        )
        assert rcpt["status"] == 1, "transaction reverted"
        return rcpt

    def revert_name(self, fn: Any, sender: str) -> str:
        """Simulate a call that must revert and return the decoded custom error name."""
        try:
            fn.call({"from": sender})
        except (ContractCustomError, ContractLogicError) as exc:
            info = self.decoder.decode(getattr(exc, "data", None))
            return info.name if info else f"undecoded: {exc}"
        raise AssertionError("expected a revert")

    def block_ts(self) -> int:
        return int(self.w3.eth.get_block("latest")["timestamp"])


def digest_of(chain_id: int, exchange: str, q: PriceQuote) -> bytes:
    s = encode_typed_data(full_message=quote_typed_data(chain_id, exchange, q))
    return bytes(keccak(b"\x19" + s.version + s.header + s.body))


def sign_order(key: str, chain_id: int, exchange: str, order: dict[str, Any]) -> bytes:
    data = {
        "types": ORDER_TYPES,
        "primaryType": "Order",
        "domain": {
            "name": "WattSwapExchange",
            "version": "1",
            "chainId": chain_id,
            "verifyingContract": exchange,
        },
        "message": order,
    }
    return bytes(Account.sign_message(encode_typed_data(full_message=data), key).signature)


def order_tuple(o: dict[str, Any]) -> tuple[Any, ...]:
    return (
        o["maker"],
        o["marketId"],
        o["side"],
        o["amountWh"],
        o["limitPrice"],
        o["expiry"],
        o["nonce"],
        o["salt"],
    )


def quote_tuple(q: PriceQuote) -> tuple[Any, ...]:
    return (bytes.fromhex(q.market_id[2:]), q.price, q.timestamp)


async def engine_tick(h: Harness, clock: FakeClock) -> Any:
    svc = make_service(h, clock)
    await svc.poll_weather_once()
    [tick] = await svc.tick_all()
    quote = svc.markets[MID].latest_quote
    assert quote is not None
    return svc, tick, quote


# ============================================================================ verification
@respx.mock
async def test_the_exchange_accepts_the_engines_quote(
    make_settings: SettingsFactory, web3_sync: Web3
) -> None:
    async with Harness(make_settings()) as h:
        clock = FakeClock()
        mock_weather(clock)
        await owner_with_meter(h)
        _, tick, quote = await engine_tick(h, clock)
        c = Chain(h, web3_sync)
        cid, ex = h.settings.chain_id, h.app.state.chain.client.exchange_address

        # 1. our EIP-712 digest is the exchange's digest
        q = quote.quote
        assert c.exchange.functions.hashQuote(quote_tuple(q)).call() == digest_of(cid, ex, q)
        # 2. the signer is an account that holds PRICER_ROLE on the PriceOracle
        pricer_role = c.oracle.functions.PRICER_ROLE().call()
        assert quote.signer == Account.from_key(KEY_PRICER).address
        assert c.oracle.functions.hasRole(pricer_role, quote.signer).call() is True
        assert c.oracle.functions.hasRole(pricer_role, Account.create().address).call() is False
        # 3. the integer price is exactly what was signed, in mUSDC base units per kWh
        assert q.price == tick.price and isinstance(q.price, int) and q.market_id == MID
        assert c.exchange.functions.marketEnabled(bytes.fromhex(MID[2:])).call() is True


# ============================================================================ a real settlement
async def prepare_trade(h: Harness, c: Chain, price: int) -> dict[str, Any]:
    """A seller holding 1.500 WATT (minted through the real meter pipeline) and a funded buyer."""
    seller_session, meter, device_key, seller_key = await owner_with_meter(h)
    r = signed_reading(h, meter, device_key, past_start(25), 1500)
    assert (await post_reading(h, r)).status_code == 202
    assert await make_worker(h).run_once() == 1
    seller = Account.from_key(seller_key).address
    assert c.token.functions.balanceOf(seller).call() == 1500

    buyer_key = "0x" + secrets.token_hex(32)
    buyer = Account.from_key(buyer_key).address
    c.send(c.usdc.functions.faucet(), buyer_key)  # 1000 mUSDC from the testnet faucet
    c.send(c.usdc.functions.approve(c.exchange.address, 2**255), buyer_key)
    c.send(c.token.functions.approve(c.exchange.address, 2**255), seller_key)
    assert seller_session  # the owner wallet signed in through SIWE earlier

    cid = h.settings.chain_id
    exp = c.block_ts() + 3600
    market = bytes.fromhex(MID[2:])
    buy = {"maker": buyer, "marketId": market, "side": 0, "amountWh": 1500, "limitPrice": price * 2,
           "expiry": exp, "nonce": 1, "salt": secrets.token_bytes(32)}  # fmt: skip
    sell = {"maker": seller, "marketId": market, "side": 1, "amountWh": 1500, "limitPrice": price // 2,
            "expiry": exp, "nonce": 1, "salt": secrets.token_bytes(32)}  # fmt: skip
    ex = h.app.state.chain.client.exchange_address
    return {
        "buy": buy, "sell": sell, "seller": seller, "buyer": buyer, "meter": meter,
        "buy_sig": sign_order(buyer_key, cid, ex, buy), "sell_sig": sign_order(seller_key, cid, ex, sell),
    }  # fmt: skip


def settle_fn(
    c: Chain, t: dict[str, Any], fill: int, exec_price: int, quote: PriceQuote, quote_sig: str
) -> Any:
    return c.exchange.functions.settle(
        order_tuple(t["buy"]), t["buy_sig"], order_tuple(t["sell"]), t["sell_sig"],
        fill, exec_price, quote_tuple(quote), bytes.fromhex(quote_sig[2:]),
    )  # fmt: skip


@respx.mock
async def test_a_trade_settles_at_the_engines_price_and_moves_exactly_the_formula_amounts(
    make_settings: SettingsFactory, web3_sync: Web3
) -> None:
    async with Harness(make_settings()) as h:
        clock = FakeClock()
        mock_weather(clock)
        await owner_with_meter(h)  # supply for the engine
        _, tick, quote = await engine_tick(h, clock)
        c = Chain(h, web3_sync)
        t = await prepare_trade(h, c, tick.price)
        treasury = c.exchange.functions.treasury().call()
        fee_bps = c.exchange.functions.feeBps().call()

        before = {
            "buyer_usdc": c.usdc.functions.balanceOf(t["buyer"]).call(),
            "seller_usdc": c.usdc.functions.balanceOf(t["seller"]).call(),
            "treasury_usdc": c.usdc.functions.balanceOf(treasury).call(),
            "buyer_watt": c.token.functions.balanceOf(t["buyer"]).call(),
            "seller_watt": c.token.functions.balanceOf(t["seller"]).call(),
        }
        fill = 1500  # Wh = 1.500 WATT = 1.5 kWh
        fn = settle_fn(c, t, fill, tick.price, quote.quote, quote.signature)
        c.send(fn, KEY_RELAYER)

        # units: price is mUSDC base units per kWh, energy is Wh, so cost = fill * price / 1000
        cost = fill * tick.price // 1000
        fee = cost * fee_bps // 10_000
        assert c.usdc.functions.balanceOf(t["buyer"]).call() == before["buyer_usdc"] - cost
        assert c.usdc.functions.balanceOf(t["seller"]).call() == before["seller_usdc"] + cost - fee
        assert c.usdc.functions.balanceOf(treasury).call() == before["treasury_usdc"] + fee
        assert c.token.functions.balanceOf(t["buyer"]).call() == before["buyer_watt"] + fill
        assert c.token.functions.balanceOf(t["seller"]).call() == before["seller_watt"] - fill
        # sanity of the magnitude: ~0.07 mUSDC/kWh x 1.5 kWh is about 100 000 base units
        assert 20_000 * 1.5 <= cost <= 600_000 * 1.5


@respx.mock
async def test_the_exchange_rejects_every_bad_quote_with_the_expected_error(
    make_settings: SettingsFactory, web3_sync: Web3
) -> None:
    async with Harness(make_settings()) as h:
        clock = FakeClock()
        mock_weather(clock)
        await owner_with_meter(h)
        _, tick, quote = await engine_tick(h, clock)
        c = Chain(h, web3_sync)
        t = await prepare_trade(h, c, tick.price)
        cid, ex = h.settings.chain_id, h.app.state.chain.client.exchange_address
        relayer = Account.from_key(KEY_RELAYER).address
        c.fund(relayer)
        now = c.block_ts()

        def with_quote(q: PriceQuote, key: str = KEY_PRICER, exec_price: int | None = None) -> Any:
            sig = sign_quote(key, cid, ex, q).signature
            return settle_fn(c, t, 1500, exec_price or tick.price, q, sig)

        # the genuine quote passes simulation
        settle_fn(c, t, 1500, tick.price, quote.quote, quote.signature).call({"from": relayer})

        # signed by a key that is not a pricer
        stranger = "0x" + secrets.token_hex(32)
        assert (
            c.revert_name(with_quote(PriceQuote(MID, tick.price, now), stranger), relayer)
            == "InvalidQuoteSigner"
        )
        # from the future (a clock-skewed signer)
        assert (
            c.revert_name(with_quote(PriceQuote(MID, tick.price, now + 3600)), relayer)
            == "QuoteFromFuture"
        )
        # for another market
        other = "0x" + keccak(text="WATT-OTHER").hex()
        assert (
            c.revert_name(with_quote(PriceQuote(other, tick.price, now)), relayer)
            == "QuoteMarketMismatch"
        )
        # execution price more than the 5 % band away from the quote
        too_far = tick.price * 110 // 100
        assert (
            c.revert_name(with_quote(PriceQuote(MID, tick.price, now), exec_price=too_far), relayer)
            == "PriceOutsideBand"
        )
        inside = tick.price * 104 // 100
        with_quote(PriceQuote(MID, tick.price, now), exec_price=inside).call(
            {"from": relayer}
        )  # 4 % is fine

        # too old: shrink the oracle's staleness window, as an admin could
        c.send(c.oracle.functions.setMaxStaleness(10), KEY_DEPLOYER)
        try:
            old = PriceQuote(MID, tick.price, now - 60)
            assert c.revert_name(with_quote(old), relayer) == "StaleQuote"
        finally:
            c.send(c.oracle.functions.setMaxStaleness(300), KEY_DEPLOYER)


@respx.mock
async def test_a_quote_stamped_with_the_simulated_clock_would_be_stale_on_chain(
    make_settings: SettingsFactory, web3_sync: Web3
) -> None:
    """Why quote timestamps use the chain clock, never the accelerated market clock."""
    async with Harness(make_settings(pricing_clock_mode="follow_readings")) as h:
        clock = FakeClock()
        mock_weather(clock)
        _, meter, device_key, _ = await owner_with_meter(h)
        r = signed_reading(h, meter, device_key, past_start(40), 400)
        assert (await post_reading(h, r)).status_code == 202
        svc = make_service(h, clock)
        [tick] = await svc.tick_all()
        quote = svc.markets[MID].latest_quote
        assert quote is not None
        assert (
            tick.market_time == r.interval_start + INTERVAL < int(time.time()) - 3600
        )  # simulated, in the past
        c = Chain(h, web3_sync)
        assert quote.quote.timestamp >= c.block_ts() - 120  # real/chain time: fresh on-chain
        assert quote.quote.timestamp > tick.market_time + 3600


# ================================================================================ the oracle
@respx.mock
async def test_the_oracle_is_updated_on_threshold_and_heartbeat_not_every_tick(
    make_settings: SettingsFactory, web3_sync: Web3
) -> None:
    async with Harness(make_settings()) as h:
        clock = FakeClock()
        mock_weather(clock)
        await owner_with_meter(h)
        svc, tick, _ = await engine_tick(h, clock)
        c = Chain(h, web3_sync)

        assert await svc.push_oracle_once() == "pushed"
        price, ts = c.oracle.functions.latest().call()
        assert (
            price == tick.price
            and ts <= c.block_ts()
            and c.oracle.functions.isFresh().call() is True
        )
        assert svc.markets[MID].oracle.last_result == "ok"

        assert await svc.push_oracle_once() == "not_due"  # same price, just pushed
        clock.advance(31)
        assert (
            await svc.push_oracle_once() == "not_due"
        )  # past the minimum interval but price unchanged

        # a move below the 50 bps threshold is not worth the gas
        rt = svc.markets[MID]
        assert rt.latest is not None
        rt.latest = replace(rt.latest, price=tick.price + tick.price * 30 // 10_000)
        assert await svc.push_oracle_once() == "not_due"

        # a 1 % move is
        await asyncio.sleep(1.1)
        c.w3.provider.make_request("evm_mine", [])  # type: ignore[arg-type]  # a new block, so a new timestamp
        rt.latest = replace(rt.latest, price=tick.price + tick.price // 100)
        assert await svc.push_oracle_once() == "pushed"
        assert c.oracle.functions.latest().call()[0] == tick.price + tick.price // 100

        # ... and a quiet market still refreshes the oracle on the heartbeat (120 s)
        await asyncio.sleep(1.1)
        c.w3.provider.make_request("evm_mine", [])  # type: ignore[arg-type]
        clock.advance(121)
        assert await svc.push_oracle_once() == "pushed"
        assert c.oracle.functions.latest().call()[1] > ts


@respx.mock
async def test_oracle_push_failures_back_off_and_never_break_ticking(
    make_settings: SettingsFactory, web3_sync: Web3
) -> None:
    async with Harness(make_settings()) as h:
        clock = FakeClock()
        mock_weather(clock)
        await owner_with_meter(h)
        svc, tick, _ = await engine_tick(h, clock)
        client = h.app.state.chain.client
        # a pricer that does NOT hold PRICER_ROLE: every update reverts
        svc.pricer = TxSender(
            client, client.account(KEY_RELAYER), h.app.state.redis, "pricer-wrong"
        )
        assert await svc.push_oracle_once() == "failed"
        o = svc.markets[MID].oracle
        assert o.failures == 1 and o.last_result.startswith("failed")
        assert await svc.push_oracle_once() == "backoff"  # no retry storm
        clock.advance(5)
        assert await svc.push_oracle_once() == "failed" and o.failures == 2  # backoff doubles

        # ticking is unaffected, and the failure is reported to the admin state
        clock.advance(15)
        [t2] = await svc.tick_all()
        assert t2.price > 0

        # fix the key: the next attempt succeeds and the failure state clears
        svc.pricer = TxSender(client, client.account(KEY_PRICER), h.app.state.redis, "pricer")
        clock.advance(120)
        assert await svc.push_oracle_once() == "pushed"
        assert o.failures == 0 and o.last_result == "ok"
        assert tick.price > 0


async def test_oracle_pusher_is_a_noop_without_a_pricer_or_a_tick(
    make_settings: SettingsFactory,
) -> None:
    async with Harness(make_settings()) as h:
        clock = FakeClock()
        svc = make_service(h, clock, with_pricer=False)
        assert await svc.push_oracle_once() == "disabled"
        svc2 = make_service(h, clock)
        assert await svc2.push_oracle_once() == "no_primary"  # no market exists yet
        async with h.app.state.sessionmaker() as s:
            await repo.ensure_default_market(s)
        assert await svc2.push_oracle_once() == "no_tick"  # a market, but no tick yet


@pytest.mark.parametrize("bad", ["0x1234", 1.5, True, 0])
def test_quote_construction_guards_are_enforced_at_the_signing_boundary(bad: object) -> None:
    with pytest.raises((TypeError, ValueError)):
        PriceQuote(MID, bad, 1_800_000_000)  # type: ignore[arg-type]
