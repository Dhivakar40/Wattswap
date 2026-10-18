"""Gas-price ceiling: bumps are clamped, a network price above the ceiling holds the tx (not sent),
and a held settlement never outlives its quote."""

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest
from web3 import Web3

from app.chain.tx import FeeCeilingError, TxSender
from app.core.metrics import TX_HELD
from app.models import SettlementStatus
from tests.conftest import SettingsFactory
from tests.integration.test_trading_settlement import pair, relayer_nonce, stack
from tests.trading_kit import Chain, build, settle_all, settlements

GWEI = 10**9


class FakeEth:
    def __init__(self, base: int, prio: int) -> None:
        self.base, self.prio = base, prio

    async def get_block(self, _: str) -> dict[str, int]:
        return {"baseFeePerGas": self.base}

    @property
    async def max_priority_fee(self) -> int:
        return self.prio


def sender(base_gwei: float, prio_gwei: float, max_fee: float, max_prio: float) -> TxSender:
    client: Any = SimpleNamespace(
        w3=SimpleNamespace(eth=FakeEth(int(base_gwei * GWEI), int(prio_gwei * GWEI))),
        settings=SimpleNamespace(max_fee_per_gas_gwei=max_fee, max_priority_fee_gwei=max_prio),
    )
    return TxSender(client, SimpleNamespace(address="0x0"), None, "test")  # type: ignore[arg-type]


async def test_normal_fees_are_unchanged_below_the_ceiling() -> None:
    fees = await sender(10, 1, 100, 5)._fees(1.0)
    assert fees == {"maxPriorityFeePerGas": 1 * GWEI, "maxFeePerGas": 21 * GWEI}


async def test_a_bump_is_clamped_at_the_ceiling_and_never_exceeds_it() -> None:
    s = sender(10, 1, 30, 2)  # network max fee = 21 gwei, ceiling 30
    bumped = await s._fees(1.25)  # wants 26.25 + tip bump: fits
    assert bumped["maxFeePerGas"] <= 30 * GWEI and bumped["maxFeePerGas"] > 21 * GWEI
    clamped = await s._fees(1.25**2)  # wants ~32.8 gwei: clamped to exactly the ceiling
    assert clamped["maxFeePerGas"] == 30 * GWEI
    assert clamped["maxPriorityFeePerGas"] <= 2 * GWEI <= clamped["maxFeePerGas"]
    for k in range(1, 4):  # whatever the bump, never above the ceiling
        try:
            f = await s._fees(1.25**k)
        except FeeCeilingError:
            continue
        assert f["maxFeePerGas"] <= 30 * GWEI and f["maxPriorityFeePerGas"] <= 2 * GWEI


async def test_a_bump_that_cannot_outbid_the_previous_attempt_is_refused() -> None:
    s = sender(10, 1, 30, 2)
    before = TX_HELD.labels("test", "bump_at_ceiling")._value.get()
    with pytest.raises(FeeCeilingError) as exc:
        await s._fees(
            1.25**4
        )  # the previous step (1.25**3 ~ 41 gwei) was already above the ceiling
    assert exc.value.reason == "bump_at_ceiling"
    assert TX_HELD.labels("test", "bump_at_ceiling")._value.get() == before + 1


async def test_a_network_price_above_the_ceiling_holds_the_tx() -> None:
    s = sender(80, 1, 100, 5)  # network max fee 161 gwei > 100
    before = TX_HELD.labels("test", "network_above_ceiling")._value.get()
    with pytest.raises(FeeCeilingError) as exc:
        await s._fees(1.0)
    assert exc.value.reason == "network_above_ceiling"
    assert TX_HELD.labels("test", "network_above_ceiling")._value.get() == before + 1


async def test_priority_fee_is_capped_not_held() -> None:
    fees = await sender(10, 9, 100, 2)._fees(1.0)
    assert fees["maxPriorityFeePerGas"] == 2 * GWEI


# ---------------------------------------------------------------- the relayer, on Anvil
async def test_settlement_is_held_not_sent_above_the_ceiling_then_sent_when_gas_falls(
    make_settings: SettingsFactory, chain: dict[str, str], web3_sync: Web3
) -> None:
    # ceiling of 1 wei: any real network price is above it
    async with stack(
        make_settings, chain, web3_sync, max_fee_per_gas_gwei=1e-9, max_priority_fee_gwei=1e-9,
        settle_hold_retry_seconds=0.05,
    ) as (h, c):  # fmt: skip
        await pair(h, c)
        b = build(h)
        await b.matcher.run_once()
        nonce = relayer_nonce(h, c)
        cycle = await b.relayer.run_once()
        assert any(n.startswith("held:network_above_ceiling") for n in cycle.notes)
        (st,) = await settlements(h)
        assert st.status == SettlementStatus.MATCHED and st.raw_tx is None  # not signed, not sent
        assert relayer_nonce(h, c) == nonce
        assert await b.relayer._hold_expired(st.id) is False

        h.settings.max_fee_per_gas_gwei = 100.0  # gas is affordable again
        h.settings.max_priority_fee_gwei = 5.0
        await asyncio.sleep(0.1)
        await settle_all(b, c)
        (st,) = await settlements(h)
        assert st.status == SettlementStatus.CONFIRMED and relayer_nonce(h, c) == nonce + 1


async def test_a_held_settlement_is_released_before_its_quote_can_go_stale(
    make_settings: SettingsFactory, chain: dict[str, str], web3_sync: Web3
) -> None:
    async with stack(
        make_settings, chain, web3_sync, max_fee_per_gas_gwei=1e-9, max_priority_fee_gwei=1e-9,
        settle_hold_retry_seconds=0.05,
    ) as (h, c):  # fmt: skip
        _seller, _buyer, sell, buy = await pair(h, c)
        b = build(h)
        await b.matcher.run_once()
        await b.relayer.run_once()  # held
        (st,) = await settlements(h)
        # pretend it has been held for as long as a quote stays usable (300 s - 60 s margin)
        b.relayer._held_since[st.id] -= 241
        nonce = relayer_nonce(h, c)
        await asyncio.sleep(0.1)  # the hold's retry delay
        cycle = await b.relayer.run_once()
        (st,) = await settlements(h)
        assert cycle.released == 1 and relayer_nonce(h, c) == nonce
        assert (
            st.status == SettlementStatus.FAILED
            and st.failure_reason == "released:fee_ceiling_hold"
        )
        from tests.trading_kit import order_row

        assert int((await order_row(h, buy)).reserved_wh) == 0  # both orders are free again
        assert int((await order_row(h, sell)).reserved_wh) == 0


async def test_a_refused_bump_keeps_the_pending_transaction(
    make_settings: SettingsFactory, chain: dict[str, str], web3_sync: Web3
) -> None:
    async with stack(make_settings, chain, web3_sync, settle_bump_after_seconds=0.05) as (h, c):
        await pair(h, c)
        b = build(h)
        c.set_automine(False)
        try:
            await b.matcher.run_once()
            await b.relayer.run_once()
            (st,) = await settlements(h)
            first = (st.tx_hash, list(st.tx_hashes))

            async def at_ceiling(*_: Any, **__: Any) -> Any:
                raise FeeCeilingError("bump_at_ceiling", "cannot outbid within the ceiling")

            b.relayer.sender.submit = at_ceiling  # type: ignore[method-assign]
            await asyncio.sleep(0.1)
            cycle = await b.relayer.run_once()
            assert "bump_held:bump_at_ceiling" in cycle.notes and cycle.bumped == 0
            (st,) = await settlements(h)
            assert st.status == SettlementStatus.SUBMITTED and (st.tx_hash, st.tx_hashes) == first
        finally:
            c.set_automine(True)
        c.mine()
        await b.listener.run_once()
        assert (await settlements(h))[0].status == SettlementStatus.CONFIRMED  # the original mined


_ = Chain
