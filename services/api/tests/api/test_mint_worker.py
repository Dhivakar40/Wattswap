import asyncio
from collections.abc import AsyncIterator
from typing import Any

import pytest
from eth_account import Account
from web3 import Web3

from app.chain.tx import SignedTx
from app.models import MintStatus
from tests.conftest import KEY_ALICE, Harness, SettingsFactory
from tests.helpers import (
    INTERVAL,
    login,
    make_worker,
    mint_rows,
    new_meter,
    past_start,
    post_reading,
    registry_contract,
    signed_reading,
    token_contract,
)


@pytest.fixture
async def setup(harness: Harness) -> tuple[dict[str, Any], str, Any]:
    alice = await login(harness, KEY_ALICE)
    meter, key = await new_meter(harness, alice)
    return meter, key, alice


def minted_events(h: Harness, w3: Web3, meter_id: str) -> list[Any]:
    """ReadingMinted events for one meter (the chain is shared by the whole test session)."""
    logs = token_contract(h, w3).events.ReadingMinted().get_logs(from_block=0)
    return [e for e in logs if "0x" + bytes(e["args"]["meterId"]).hex() == meter_id]


async def test_worker_mints_in_order_and_balance_matches(
    harness: Harness, setup: Any, web3_sync: Web3
) -> None:
    meter, key, alice = setup
    t = past_start(30)
    amounts = [300, 450, 600]
    for i, wh in enumerate(amounts):
        assert (
            await post_reading(harness, signed_reading(harness, meter, key, t + i * INTERVAL, wh))
        ).status_code == 202

    done = await make_worker(harness).run_once()
    assert done == 3

    rows = await mint_rows(harness)
    assert [m.status for _, m in rows] == [MintStatus.CONFIRMED] * 3
    assert all(m.tx_hash and m.block_number and m.confirmed_at for _, m in rows)
    blocks = [m.block_number or 0 for _, m in rows]
    assert blocks == sorted(blocks)  # oldest interval minted first

    token = token_contract(harness, web3_sync)
    assert token.functions.balanceOf(Account.from_key(KEY_ALICE).address).call() == sum(amounts)
    events = minted_events(harness, web3_sync, meter["meter_id"])
    assert [e["args"]["amountWh"] for e in events] == amounts
    registry = registry_contract(harness, web3_sync)
    assert registry.functions.lastIntervalEnd(meter["meter_id"]).call() == t + 3 * INTERVAL

    # The API reports it, and a second pass has nothing left to do.
    me = await harness.client.get(f"/api/v1/meters/{meter['id']}", headers=alice.headers)
    assert me.json()["minted_wh"] == sum(amounts)
    assert await make_worker(harness).run_once() == 0
    assert len(minted_events(harness, web3_sync, meter["meter_id"])) == 3


async def test_terminal_revert_is_decoded_and_stored(harness: Harness, setup: Any) -> None:
    meter, key, alice = setup
    assert (
        await post_reading(harness, signed_reading(harness, meter, key, past_start()))
    ).status_code == 202
    # The owner deactivates the meter after the reading was accepted but before it was minted.
    off = await harness.client.patch(
        f"/api/v1/meters/{meter['id']}", json={"active": False}, headers=alice.headers
    )
    assert off.status_code == 200

    assert await make_worker(harness).run_once() == 1
    ((_, mint),) = await mint_rows(harness)
    assert mint.status == MintStatus.FAILED
    assert mint.failure_reason is not None
    assert mint.failure_reason.startswith("MeterInactive(")
    assert meter["meter_id"] in mint.failure_reason
    assert mint.tx_hash is None  # simulation caught it: no gas was spent on a doomed tx


async def test_a_failed_reading_becomes_a_gap_and_later_ones_still_mint(
    harness: Harness, setup: Any, web3_sync: Web3
) -> None:
    meter, key, _ = setup
    t = past_start(30)
    await post_reading(harness, signed_reading(harness, meter, key, t, 100))
    await post_reading(harness, signed_reading(harness, meter, key, t + INTERVAL, 200))
    # Make the first one unmintable by tightening the registry's plausibility ceiling (admin op).
    registry = registry_contract(harness, web3_sync)
    admin = Account.from_key("0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80")
    tx = registry.functions.setPlausibility(1_000, 60, 86_400).build_transaction(
        {"from": admin.address, "nonce": web3_sync.eth.get_transaction_count(admin.address)}
    )
    web3_sync.eth.wait_for_transaction_receipt(
        web3_sync.eth.send_raw_transaction(admin.sign_transaction(tx).raw_transaction)
    )
    harness.app.state.chain.client._params = None  # drop the cached plausibility params
    # ceiling is now 5000 W x 900 s x 10% / 3600 = 125 Wh: 100 passes, 200 does not.
    try:
        await make_worker(harness).run_once()
    finally:
        restore = registry.functions.setPlausibility(12_000, 60, 86_400).build_transaction(
            {"from": admin.address, "nonce": web3_sync.eth.get_transaction_count(admin.address)}
        )
        web3_sync.eth.wait_for_transaction_receipt(
            web3_sync.eth.send_raw_transaction(admin.sign_transaction(restore).raw_transaction)
        )
    rows = await mint_rows(harness)
    assert rows[0][1].status == MintStatus.CONFIRMED
    assert rows[1][1].status == MintStatus.FAILED
    assert rows[1][1].failure_reason is not None
    assert rows[1][1].failure_reason.startswith("ReadingImplausible(")


async def test_adopts_a_reading_that_is_already_minted_on_chain(
    harness: Harness, setup: Any, web3_sync: Web3
) -> None:
    """Crash after the chain minted but before the DB knew: the worker adopts, never re-mints."""
    meter, key, _ = setup
    r = signed_reading(harness, meter, key, past_start(), 700)
    assert (await post_reading(harness, r)).status_code == 202

    oracle = harness.app.state.chain.oracle
    chain = harness.app.state.chain.client
    signed = await oracle.submit(
        chain.token.functions.mint(
            (r.meter_id, r.reading_id, r.amount_wh, r.interval_start, r.interval_seconds),
            bytes.fromhex(r.signature[2:]),
        )
    )
    await oracle.wait([signed.tx_hash], 10)

    assert await make_worker(harness).run_once() == 1
    ((_, mint),) = await mint_rows(harness)
    assert mint.status == MintStatus.CONFIRMED and mint.attempts == 0  # we sent nothing
    assert mint.tx_hash == signed.tx_hash
    assert len(minted_events(harness, web3_sync, meter["meter_id"])) == 1


async def test_crash_after_signing_before_broadcast_resends_the_same_tx(
    harness: Harness, setup: Any, web3_sync: Web3
) -> None:
    meter, key, _ = setup
    assert (
        await post_reading(harness, signed_reading(harness, meter, key, past_start(), 800))
    ).status_code == 202

    worker = make_worker(harness)
    real_broadcast = worker.sender.broadcast

    async def crash(signed: SignedTx) -> None:
        raise ConnectionError("process died before the tx reached the node")

    worker.sender.broadcast = crash  # type: ignore[method-assign]
    await worker.run_once()
    ((_, mint),) = await mint_rows(harness)
    assert mint.status == MintStatus.SUBMITTED and mint.raw_tx and mint.tx_hash
    persisted_hash = mint.tx_hash
    assert minted_events(harness, web3_sync, meter["meter_id"]) == []  # nothing reached the chain

    # "Restart": a brand-new worker finds the SUBMITTED row and re-broadcasts the persisted tx.
    worker.sender.broadcast = real_broadcast  # type: ignore[method-assign]
    fresh = make_worker(harness)
    for _ in range(20):
        if await fresh.run_once():
            break
        await asyncio.sleep(0.2)
    ((_, mint),) = await mint_rows(harness)
    assert mint.status == MintStatus.CONFIRMED
    assert mint.tx_hash == persisted_hash  # the same signed tx, not a new one
    assert mint.attempts == 1
    events = minted_events(harness, web3_sync, meter["meter_id"])
    assert len(events) == 1 and events[0]["args"]["amountWh"] == 800


async def test_crash_after_broadcast_before_confirmation_is_picked_up(
    harness: Harness, setup: Any, web3_sync: Web3
) -> None:
    meter, key, _ = setup
    assert (
        await post_reading(harness, signed_reading(harness, meter, key, past_start(), 650))
    ).status_code == 202
    worker = make_worker(harness)

    # Submit and persist, but pretend the process died before observing the receipt.
    original = worker._drive

    async def no_drive(mint_id: object, *, wait: bool = False) -> bool:
        return False

    worker._drive = no_drive  # type: ignore[method-assign]
    await worker.run_once()
    ((_, mint),) = await mint_rows(harness)
    assert mint.status == MintStatus.SUBMITTED

    worker._drive = original  # type: ignore[method-assign]
    assert await make_worker(harness).run_once() == 1
    ((_, mint),) = await mint_rows(harness)
    assert mint.status == MintStatus.CONFIRMED
    assert len(minted_events(harness, web3_sync, meter["meter_id"])) == 1


async def test_stuck_transaction_is_replaced_with_a_higher_fee(
    make_settings: SettingsFactory, web3_sync: Web3
) -> None:
    async with Harness(make_settings(mint_tx_timeout_seconds=1.0)) as h:
        alice = await login(h, KEY_ALICE)
        meter, key = await new_meter(h, alice)
        assert (
            await post_reading(h, signed_reading(h, meter, key, past_start(), 900))
        ).status_code == 202

        web3_sync.provider.make_request("evm_setAutomine", [False])  # type: ignore[arg-type]
        try:
            worker = make_worker(h)
            await worker.run_once()  # signs + broadcasts; nothing mines it
            ((_, mint),) = await mint_rows(h)
            first_hash, first_attempts = mint.tx_hash, mint.attempts
            assert mint.status == MintStatus.SUBMITTED and first_attempts >= 1

            await asyncio.sleep(1.3)  # exceed the (1 s) timeout
            await worker.run_once()  # bumps the fee, same nonce
            ((_, mint),) = await mint_rows(h)
            assert mint.attempts > first_attempts and mint.tx_hash != first_hash
        finally:
            web3_sync.provider.make_request("evm_setAutomine", [True])  # type: ignore[arg-type]
        web3_sync.provider.make_request("evm_mine", [])  # type: ignore[arg-type]

        for _ in range(20):
            if await make_worker(h).run_once():
                break
            await asyncio.sleep(0.2)
        ((_, mint),) = await mint_rows(h)
        assert mint.status == MintStatus.CONFIRMED
        events = minted_events(h, web3_sync, meter["meter_id"])
        assert len(events) == 1 and events[0]["args"]["amountWh"] == 900


async def test_two_workers_never_double_mint(
    make_settings: SettingsFactory, web3_sync: Web3
) -> None:
    """Only the holder of the Redis leader lock mints, so concurrent workers are safe."""
    async with Harness(make_settings()) as h:
        alice = await login(h, KEY_ALICE)
        meter, key = await new_meter(h, alice)
        t = past_start(30)
        for i in range(4):
            assert (
                await post_reading(h, signed_reading(h, meter, key, t + i * INTERVAL, 100 + i))
            ).status_code == 202

        stop = asyncio.Event()
        tasks = [asyncio.create_task(make_worker(h).run(stop)) for _ in range(2)]
        try:
            for _ in range(100):
                rows = await mint_rows(h)
                if all(m.status == MintStatus.CONFIRMED for _, m in rows):
                    break
                await asyncio.sleep(0.2)
        finally:
            stop.set()
            await asyncio.gather(*tasks)
        assert [m.status for _, m in await mint_rows(h)] == [MintStatus.CONFIRMED] * 4
        events = minted_events(h, web3_sync, meter["meter_id"])
        assert [e["args"]["amountWh"] for e in events] == [100, 101, 102, 103]


async def test_background_worker_runs_from_the_app_lifespan(
    make_settings: SettingsFactory, web3_sync: Web3
) -> None:
    async with Harness(make_settings(mint_worker_enabled=True)) as h:
        alice = await login(h, KEY_ALICE)
        meter, key = await new_meter(h, alice)
        assert (
            await post_reading(h, signed_reading(h, meter, key, past_start(), 420))
        ).status_code == 202
        for _ in range(100):
            if all(m.status == MintStatus.CONFIRMED for _, m in await mint_rows(h)):
                break
            await asyncio.sleep(0.2)
        ((_, mint),) = await mint_rows(h)
        assert mint.status == MintStatus.CONFIRMED
        mints = (await h.client.get("/api/v1/mints", headers=alice.headers)).json()
        assert mints["items"][0]["mint"]["status"] == "confirmed"
        assert (await h.client.get("/metrics")).text.count("wattswap_mints_total") >= 1


@pytest.fixture(autouse=True)
async def _no_leaked_automine_off(web3_sync: Web3) -> AsyncIterator[None]:
    yield
    web3_sync.provider.make_request("evm_setAutomine", [True])  # type: ignore[arg-type]
