"""Only a contract-level rejection may mark a reading failed. Everything else retries forever.

Infrastructure trouble (RPC down, timeouts, nonce or gas problems, undecodable reverts) must
leave the reading pending/submitted with no failure recorded, and it must still mint once the
trouble clears.
"""

import asyncio
from typing import Any

import pytest
from eth_account import Account
from web3 import Web3

import app.chain.tx as txmod
from app.chain.revert import RevertInfo
from app.chain.tx import SignedTx, TxRevertedError, is_stale_nonce
from app.models import MintStatus
from app.workers.mint_worker import MintWorker, is_contract_rejection
from tests.conftest import KEY_ALICE, KEY_ORACLE, Harness, SettingsFactory
from tests.helpers import (
    INTERVAL,
    login,
    make_worker,
    mint_rows,
    new_meter,
    past_start,
    post_reading,
    signed_reading,
    token_contract,
)


def minted_events(h: Harness, w3: Web3, meter_id: str) -> list[Any]:
    logs = token_contract(h, w3).events.ReadingMinted().get_logs(from_block=0)
    return [e for e in logs if "0x" + bytes(e["args"]["meterId"]).hex() == meter_id]


class RpcOutage:
    """Point the chain client at a dead port: real connection errors, like a down RPC node."""

    DEAD = "http://127.0.0.1:9"  # nothing listens on the discard port

    def __init__(self, h: Harness) -> None:
        self.provider: Any = h.app.state.chain.client.w3.provider
        self.live: str = self.provider.endpoint_uri
        # No library-level retries: we want to observe the worker's own retry and backoff.
        self.provider.exception_retry_configuration = None

    def down(self) -> None:
        self.provider.endpoint_uri = self.DEAD

    def up(self) -> None:
        self.provider.endpoint_uri = self.live


def fast_retry(make_settings: SettingsFactory) -> Any:
    return make_settings(mint_backoff_base_seconds=0.01, mint_backoff_max_seconds=0.05)


async def drive_until_confirmed(h: Harness, worker: MintWorker, tries: int = 60) -> None:
    for _ in range(tries):
        await worker.run_once()
        rows = await mint_rows(h)
        if rows and all(m.status == MintStatus.CONFIRMED for _, m in rows):
            return
        await asyncio.sleep(0.1)
    state = [(m.status, m.failure_reason) for _, m in await mint_rows(h)]
    pytest.fail(f"never confirmed: {state}")


async def queued(h: Harness, wh: int) -> tuple[dict[str, Any], str]:
    alice = await login(h, KEY_ALICE)
    meter, key = await new_meter(h, alice)
    resp = await post_reading(h, signed_reading(h, meter, key, past_start(), wh))
    assert resp.status_code == 202
    return meter, key


# ------------------------------------------------------------------------- classification
def test_only_decoded_contract_errors_are_rejections() -> None:
    assert is_contract_rejection(RevertInfo("MeterInactive", {"meterId": "0x1"}))
    assert is_contract_rejection(RevertInfo("ReadingImplausible", {"amountWh": 2, "maxWh": 1}))
    assert is_contract_rejection(RevertInfo("InvalidDeviceSignature"))
    assert is_contract_rejection(RevertInfo("Panic", {"code": "0x11"}))
    # Not verdicts on the reading:
    assert not is_contract_rejection(None)
    assert not is_contract_rejection(RevertInfo("UnknownError", {"selector": "0xdeadbeef"}))
    assert not is_contract_rejection(RevertInfo("UndecodableRevert"))
    assert not is_contract_rejection(RevertInfo("ReadingNotComplete", {"end": 5}))  # clock-based


def test_stale_nonce_detection() -> None:
    assert is_stale_nonce(ValueError("nonce too low: next nonce 5, tx nonce 4"))
    assert is_stale_nonce(ValueError("Nonce is too low"))
    assert not is_stale_nonce(ConnectionError("connection refused"))
    assert not is_stale_nonce(TimeoutError("timed out"))


# ------------------------------------------------------------------------------- outages
async def test_rpc_outage_never_fails_a_reading_and_it_mints_afterwards(
    make_settings: SettingsFactory, web3_sync: Web3
) -> None:
    async with Harness(fast_retry(make_settings)) as h:
        meter, _ = await queued(h, 640)
        worker, outage = make_worker(h), RpcOutage(h)

        outage.down()
        for _ in range(6):  # several attempts while the RPC is unreachable
            assert await worker.run_once() == 0
            ((_, mint),) = await mint_rows(h)
            assert mint.status == MintStatus.PENDING and mint.failure_reason is None
            await asyncio.sleep(0.06)
        assert next(iter(worker._retry.values()))[0] >= 3  # it kept trying, with backoff

        outage.up()
        await drive_until_confirmed(h, worker)
        ((_, mint),) = await mint_rows(h)
        assert mint.status == MintStatus.CONFIRMED and mint.failure_reason is None
        events = minted_events(h, web3_sync, meter["meter_id"])
        assert len(events) == 1 and events[0]["args"]["amountWh"] == 640
        assert worker._retry == {}  # backoff state cleared after success


async def test_rpc_outage_after_the_tx_was_signed_still_confirms_exactly_once(
    make_settings: SettingsFactory, web3_sync: Web3
) -> None:
    async with Harness(fast_retry(make_settings)) as h:
        meter, _ = await queued(h, 710)
        worker, outage = make_worker(h), RpcOutage(h)
        real_broadcast = worker.sender.broadcast

        async def die(signed: SignedTx) -> None:
            raise ConnectionError("rpc went away mid-broadcast")

        worker.sender.broadcast = die  # type: ignore[method-assign]
        await worker.run_once()
        worker.sender.broadcast = real_broadcast  # type: ignore[method-assign]
        ((_, mint),) = await mint_rows(h)
        assert mint.status == MintStatus.SUBMITTED  # signed and persisted, never broadcast

        outage.down()
        for _ in range(5):
            await worker.run_once()
            ((_, mint),) = await mint_rows(h)
            assert mint.status == MintStatus.SUBMITTED and mint.failure_reason is None
            await asyncio.sleep(0.06)

        outage.up()
        await drive_until_confirmed(h, worker)
        assert len(minted_events(h, web3_sync, meter["meter_id"])) == 1


async def test_background_loop_survives_an_outage_and_backs_off(
    make_settings: SettingsFactory, web3_sync: Web3
) -> None:
    async with Harness(fast_retry(make_settings)) as h:
        meter, _ = await queued(h, 305)
        worker, outage = make_worker(h), RpcOutage(h)
        stop = asyncio.Event()
        outage.down()
        task = asyncio.create_task(worker.run(stop))
        try:
            await asyncio.sleep(1.0)  # many poll cycles during the outage
            ((_, mint),) = await mint_rows(h)
            assert mint.status == MintStatus.PENDING and mint.failure_reason is None
            outage.up()
            for _ in range(100):
                ((_, mint),) = await mint_rows(h)
                if mint.status == MintStatus.CONFIRMED:
                    break
                await asyncio.sleep(0.1)
            assert mint.status == MintStatus.CONFIRMED
        finally:
            stop.set()
            await task
        assert len(minted_events(h, web3_sync, meter["meter_id"])) == 1


# --------------------------------------------------------------------- nonce and gas
async def test_consumed_nonce_requeues_instead_of_failing(
    make_settings: SettingsFactory, web3_sync: Web3
) -> None:
    """The persisted tx's nonce gets used by another transaction: sign a fresh one."""
    async with Harness(fast_retry(make_settings)) as h:
        meter, _ = await queued(h, 530)
        worker = make_worker(h)
        real_broadcast = worker.sender.broadcast

        async def die(signed: SignedTx) -> None:
            raise ConnectionError("died before broadcast")

        worker.sender.broadcast = die  # type: ignore[method-assign]
        await worker.run_once()
        worker.sender.broadcast = real_broadcast  # type: ignore[method-assign]  # the "restart"
        ((_, mint),) = await mint_rows(h)
        assert mint.status == MintStatus.SUBMITTED
        stale_hash = mint.tx_hash

        # Something else (an operator, another service) spends the oracle's nonce.
        oracle = Account.from_key(KEY_ORACLE)
        tx = {
            "to": oracle.address,
            "value": 0,
            "gas": 21_000,
            "chainId": 31337,
            "nonce": web3_sync.eth.get_transaction_count(oracle.address),
            "maxFeePerGas": web3_sync.eth.gas_price * 2,
            "maxPriorityFeePerGas": 1,
        }
        web3_sync.eth.wait_for_transaction_receipt(
            web3_sync.eth.send_raw_transaction(oracle.sign_transaction(tx).raw_transaction)
        )

        await drive_until_confirmed(h, make_worker(h))  # a fresh worker = a restart
        ((_, mint),) = await mint_rows(h)
        assert mint.status == MintStatus.CONFIRMED and mint.tx_hash != stale_hash
        assert mint.failure_reason is None
        assert len(minted_events(h, web3_sync, meter["meter_id"])) == 1


async def test_mined_revert_without_a_contract_error_is_retried_not_failed(
    make_settings: SettingsFactory, web3_sync: Web3, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Out of gas mines as a reverted tx with no revert reason: a gas problem, not a verdict."""
    async with Harness(fast_retry(make_settings)) as h:
        meter, _ = await queued(h, 480)
        worker = make_worker(h)

        monkeypatch.setattr(txmod, "GAS_MARGIN", 0.5)  # gas limit far too low -> out of gas
        await worker.run_once()
        ((_, mint),) = await mint_rows(h)
        assert mint.status != MintStatus.FAILED and mint.failure_reason is None
        assert minted_events(h, web3_sync, meter["meter_id"]) == []

        monkeypatch.setattr(txmod, "GAS_MARGIN", 1.25)
        await drive_until_confirmed(h, worker)
        ((_, mint),) = await mint_rows(h)
        assert mint.status == MintStatus.CONFIRMED and mint.attempts >= 2
        assert len(minted_events(h, web3_sync, meter["meter_id"])) == 1


async def test_undecodable_revert_is_retried_not_failed(
    make_settings: SettingsFactory, web3_sync: Web3
) -> None:
    async with Harness(fast_retry(make_settings)) as h:
        meter, _ = await queued(h, 390)
        worker = make_worker(h)
        real = worker.sender.simulate

        async def opaque(fn: Any) -> int:
            raise TxRevertedError(None, "execution reverted")  # no decodable contract error

        worker.sender.simulate = opaque  # type: ignore[method-assign]
        for _ in range(4):
            await worker.run_once()
            ((_, mint),) = await mint_rows(h)
            assert mint.status == MintStatus.PENDING and mint.failure_reason is None
            await asyncio.sleep(0.06)
        worker.sender.simulate = real  # type: ignore[method-assign]
        await drive_until_confirmed(h, worker)
        assert len(minted_events(h, web3_sync, meter["meter_id"])) == 1


# ------------------------------------------------------------------- visibility of gaps
async def test_failed_readings_and_their_revert_reason_are_visible_to_the_owner(
    make_settings: SettingsFactory,
) -> None:
    async with Harness(fast_retry(make_settings)) as h:
        alice = await login(h, KEY_ALICE)
        meter, key = await new_meter(h, alice)
        t = past_start(30)
        for i in range(2):
            r = signed_reading(h, meter, key, t + i * INTERVAL, 100)
            assert (await post_reading(h, r)).status_code == 202
        # Contract-level rejection: the meter is deactivated before the mints run.
        await h.client.patch(
            f"/api/v1/meters/{meter['id']}", json={"active": False}, headers=alice.headers
        )
        await make_worker(h).run_once()

        page = (
            await h.client.get(f"/api/v1/meters/{meter['id']}/readings", headers=alice.headers)
        ).json()
        assert len(page["items"]) == 2
        for item in page["items"]:
            assert item["mint"]["status"] == "failed"
            assert item["mint"]["failure_reason"].startswith("MeterInactive(meterId=")
            assert item["mint"]["tx_hash"] is None
        mints = (await h.client.get("/api/v1/mints", headers=alice.headers)).json()["items"]
        assert {m["mint"]["status"] for m in mints} == {"failed"}
