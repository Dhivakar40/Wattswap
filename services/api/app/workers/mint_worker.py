"""Mint worker: pending readings -> EnergyToken.mint, in order, exactly once.

State machine (persisted in `mints`):

    pending --(tx signed; raw tx + hash saved BEFORE broadcast)--> submitted
    submitted --(receipt ok, N confirmations)--> confirmed
    submitted --(receipt reverted)--> failed
    pending --(simulation reverts with a terminal custom error)--> failed

Exactly-once is layered: DB unique constraints, an on-chain `readingMinted` check before every
send, a persisted signed tx that is re-broadcast (never re-signed with a new nonce) after a
crash, and the contract's own replay protection.

Readings of one meter are handled strictly oldest-first because the contract rejects any
interval that starts before the previous one ended.
"""

import asyncio
import logging
import time
from datetime import UTC, datetime

import redis.asyncio as aioredis
from redis.exceptions import LockError
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from web3.contract.async_contract import AsyncContractFunction
from web3.exceptions import TransactionNotFound

from app.chain.client import ChainClient
from app.chain.revert import RevertInfo
from app.chain.tx import SignedTx, TxRevertedError, TxSender, is_stale_nonce
from app.core.config import Settings
from app.core.metrics import MINTS, MINTS_PENDING
from app.models import Meter, Mint, MintStatus, Reading

log = logging.getLogger("wattswap.mint_worker")

LEADER_LOCK = "lock:mint-worker"
FEE_BUMP_STEP = 1.25


# A revert is a verdict on the READING only if the contract itself rejected it with a decodable error.
_NOT_A_VERDICT = frozenset({"UnknownError", "UndecodableRevert"})


def is_contract_rejection(info: RevertInfo | None) -> bool:
    """True only for a decoded contract-level rejection (MeterInactive, ReadingImplausible, ...).

    Not a rejection: no revert data, undecodable data, or a time-dependent error such as
    ReadingNotComplete (it clears by itself once the chain clock passes the interval end).
    """
    return info is not None and info.name not in _NOT_A_VERDICT and not info.transient


class MintWorker:
    def __init__(
        self,
        settings: Settings,
        sessionmaker: async_sessionmaker[AsyncSession],
        redis: aioredis.Redis,
        client: ChainClient,
        sender: TxSender,
    ) -> None:
        self.settings = settings
        self.sessionmaker = sessionmaker
        self.redis = redis
        self.client = client
        self.sender = sender
        # In-memory retry state per mint: (consecutive failures, monotonic time it may run again).
        self._retry: dict[object, tuple[int, float]] = {}

    # ------------------------------------------------------------------ retry/backoff
    def _backoff(self, failures: int) -> float:
        s = self.settings
        return float(
            min(s.mint_backoff_max_seconds, s.mint_backoff_base_seconds * 2 ** (failures - 1))
        )

    def _defer(self, mint_id: object, why: str) -> None:
        """Infrastructure trouble (RPC, nonce, gas, timeouts): retry later, never fail the mint."""
        failures = self._retry.get(mint_id, (0, 0.0))[0] + 1
        delay = self._backoff(failures)
        self._retry[mint_id] = (failures, time.monotonic() + delay)
        log.warning(
            "mint deferred, will retry",
            extra={"mint_id": str(mint_id), "why": why, "failures": failures, "retry_in_s": delay},
        )

    def _due(self, mint_id: object) -> bool:
        return time.monotonic() >= self._retry.get(mint_id, (0, 0.0))[1]

    # ------------------------------------------------------------------ main loop
    async def run(self, stop: asyncio.Event) -> None:
        """Single-writer loop: only the holder of the Redis leader lock mints."""
        lock = self.redis.lock(LEADER_LOCK, timeout=30)
        leader = False
        loop_failures = 0
        try:
            while not stop.is_set():
                pause = self.settings.mint_poll_seconds
                try:
                    if not leader:
                        leader = await lock.acquire(blocking=False)
                        if leader:
                            log.info("mint worker became leader")
                    if leader:
                        await lock.reacquire()
                        await self.run_once()
                    loop_failures = 0
                except LockError:
                    leader = False
                    log.warning("mint worker lost leader lock")
                except Exception:
                    loop_failures += 1
                    pause = max(pause, self._backoff(loop_failures))
                    log.exception("mint worker iteration failed", extra={"retry_in_s": pause})
                try:
                    await asyncio.wait_for(stop.wait(), pause)
                except TimeoutError:
                    pass
        finally:
            if leader:
                try:
                    await lock.release()
                except LockError:
                    pass

    async def run_once(self) -> int:
        """One pass: resolve in-flight txs, then mint pending readings oldest-first.

        Returns the number of mints that reached a terminal state in this pass.
        """
        done = 0
        async with self.sessionmaker() as s:
            submitted = (
                await s.scalars(select(Mint.id).where(Mint.status == MintStatus.SUBMITTED))
            ).all()
        for mint_id in submitted:
            if not self._due(mint_id):
                continue
            try:
                if await self._drive(mint_id):
                    done += 1
                    self._retry.pop(mint_id, None)
            except Exception as exc:  # RPC down, node rejected something, ...: retry, never fail
                self._defer(mint_id, f"{type(exc).__name__}: {exc}")

        async with self.sessionmaker() as s:
            rows = (
                await s.execute(
                    select(Mint.id, Reading.meter_pk)
                    .join(Reading, Reading.id == Mint.reading_pk)
                    .where(Mint.status == MintStatus.PENDING)
                    .order_by(Reading.meter_pk, Reading.interval_start)
                )
            ).all()
        blocked: set[object] = set()
        for mint_id, meter_pk in rows:
            if meter_pk in blocked:
                continue  # keep per-meter order: an earlier reading is still unresolved
            if not self._due(mint_id):
                blocked.add(meter_pk)
                continue
            try:
                outcome = await self._process_pending(mint_id)
            except Exception as exc:  # RPC down etc.: the mint stays pending and is retried
                self._defer(mint_id, f"{type(exc).__name__}: {exc}")
                blocked.add(meter_pk)
                continue
            if outcome == "done":
                done += 1
                self._retry.pop(mint_id, None)
            else:
                blocked.add(meter_pk)
                if outcome == "retry":
                    self._defer(mint_id, "transient")

        async with self.sessionmaker() as s:
            pending = await s.scalar(
                select(func.count())
                .select_from(Mint)
                .where(Mint.status.in_([MintStatus.PENDING, MintStatus.SUBMITTED]))
            )
        MINTS_PENDING.set(int(pending or 0))
        return done

    # ------------------------------------------------------------------ helpers
    async def _load(self, s: AsyncSession, mint_id: object) -> tuple[Mint, Reading, Meter]:
        row = (
            await s.execute(
                select(Mint, Reading, Meter)
                .join(Reading, Reading.id == Mint.reading_pk)
                .join(Meter, Meter.id == Reading.meter_pk)
                .where(Mint.id == mint_id)
            )
        ).one()
        return row[0], row[1], row[2]

    def _mint_fn(self, reading: Reading, meter: Meter) -> AsyncContractFunction:
        return self.client.token.functions.mint(
            (
                meter.meter_id,
                reading.reading_id,
                reading.amount_wh,
                reading.interval_start,
                reading.interval_seconds,
            ),
            bytes.fromhex(reading.signature[2:]),
        )

    async def _finish_confirmed(
        self, s: AsyncSession, mint: Mint, block_number: int | None, tx_hash: str | None
    ) -> None:
        mint.status = MintStatus.CONFIRMED
        mint.block_number = block_number
        mint.tx_hash = tx_hash or mint.tx_hash
        mint.failure_reason = None
        mint.confirmed_at = datetime.now(UTC)
        await s.commit()
        MINTS.labels(status="confirmed").inc()
        log.info("mint confirmed", extra={"tx": mint.tx_hash, "block": block_number})

    async def _finish_failed(self, s: AsyncSession, mint: Mint, reason: str) -> None:
        mint.status = MintStatus.FAILED
        mint.failure_reason = reason[:1000]
        await s.commit()
        MINTS.labels(status="failed").inc()
        log.warning("mint failed", extra={"reason": reason, "mint_id": str(mint.id)})

    async def _requeue(self, s: AsyncSession, mint: Mint) -> None:
        """Back to pending with the dead transaction forgotten (attempts is kept for audit)."""
        mint.status = MintStatus.PENDING
        mint.tx_hash = None
        mint.raw_tx = None
        mint.tx_nonce = None
        await s.commit()

    async def _adopt_existing(self, s: AsyncSession, mint: Mint, reading: Reading) -> bool:
        """If the chain already minted this reading (crash after mining), adopt it."""
        if not await self.client.reading_minted(reading.reading_id):
            return False
        tx_hash, block = await self._find_mint_event(reading.reading_id)
        await self._finish_confirmed(s, mint, block, tx_hash)
        log.info("adopted on-chain mint", extra={"reading_id": reading.reading_id})
        return True

    async def _find_mint_event(self, reading_id: str) -> tuple[str | None, int | None]:
        try:
            logs = await self.client.token.events.ReadingMinted.get_logs(
                from_block=0, argument_filters={"readingId": reading_id}
            )
        except Exception:  # large ranges can be rejected by public RPCs; the hash is cosmetic
            return None, None
        if not logs:
            return None, None
        return "0x" + bytes(logs[0]["transactionHash"]).hex(), int(logs[0]["blockNumber"])

    # ------------------------------------------------------------------ pending -> submitted
    async def _process_pending(self, mint_id: object) -> str:
        """Returns 'done' if the mint reached a terminal state, else 'wait' (keep order)."""
        async with self.sessionmaker() as s:
            mint, reading, meter = await self._load(s, mint_id)
            if mint.status != MintStatus.PENDING:
                return "wait"
            if await self._adopt_existing(s, mint, reading):
                return "done"

            fn = self._mint_fn(reading, meter)

            async def persist(signed: SignedTx) -> None:
                mint.status = MintStatus.SUBMITTED
                mint.tx_hash = signed.tx_hash
                mint.raw_tx = signed.raw
                mint.tx_nonce = signed.nonce
                mint.attempts += 1
                await s.commit()  # durable BEFORE the tx is broadcast

            try:
                await self.sender.submit(fn, on_signed=persist)
            except TxRevertedError as exc:
                info = exc.info
                if info is not None and info.name == "ReadingAlreadyMinted":
                    # Raced with an earlier attempt that landed: adopt it, never fail it.
                    return "done" if await self._adopt_existing(s, mint, reading) else "retry"
                if is_contract_rejection(info):
                    await self._finish_failed(s, mint, str(exc))
                    return "done"
                # Time-dependent (ReadingNotComplete) or not decodable into a contract error:
                # not a verdict on the reading. Keep it pending and retry with backoff.
                log.info("mint not rejected by the contract, will retry: %s", exc)
                return "retry"
            except Exception as exc:
                log.warning("mint submit failed; will retry: %s", exc)
                # If `persist` already ran, the row is SUBMITTED and _drive will re-broadcast.
                return "retry"
            mint_pk = mint.id
        return "done" if await self._drive(mint_pk, wait=True) else "wait"

    # ------------------------------------------------------------------ submitted -> terminal
    async def _drive(self, mint_id: object, *, wait: bool = False) -> bool:
        """Advance a SUBMITTED mint. With wait=True, poll until terminal or timeout.

        Returns True if the mint reached a terminal state.
        """
        deadline = time.monotonic() + (self.settings.mint_tx_timeout_seconds if wait else 0)
        while True:
            async with self.sessionmaker() as s:
                mint, reading, meter = await self._load(s, mint_id)
                if mint.status != MintStatus.SUBMITTED:
                    return mint.status in (MintStatus.CONFIRMED, MintStatus.FAILED)
                assert mint.tx_hash is not None and mint.raw_tx is not None
                if await self._resolve(s, mint, reading, meter):
                    return True
            if time.monotonic() >= deadline:
                return False
            await asyncio.sleep(0.25)

    async def _resolve(self, s: AsyncSession, mint: Mint, reading: Reading, meter: Meter) -> bool:
        assert mint.tx_hash is not None and mint.raw_tx is not None
        rcpt = await self.sender.receipt(mint.tx_hash)
        if rcpt is not None:
            head = int(await self.client.w3.eth.block_number)
            block = int(rcpt["blockNumber"])
            if head < block + self.settings.mint_confirmations - 1:
                return False  # mined, waiting for confirmations
            if int(rcpt["status"]) == 1:
                await self._finish_confirmed(s, mint, block, mint.tx_hash)
            else:
                info = await self.sender.revert_reason(mint.tx_hash)
                if is_contract_rejection(info):
                    await self._finish_failed(s, mint, str(info))
                    return True
                # Mined but reverted with no contract error (e.g. out of gas, or state that has
                # since changed): not a verdict on the reading. Re-sign from scratch.
                log.warning("mined tx reverted without a contract error; re-queueing")
                await self._requeue(s, mint)
                return False

        # No receipt for our tx. A previous (fee-bumped) attempt may have been mined instead.
        if await self._adopt_existing(s, mint, reading):
            return True

        # Make sure the node still knows the tx (crash between persisting and broadcasting).
        try:
            await self.client.w3.eth.get_transaction(mint.tx_hash)  # type: ignore[arg-type]
        except TransactionNotFound:
            log.info("re-broadcasting persisted tx", extra={"tx": mint.tx_hash})
            try:
                await self.sender.broadcast(
                    SignedTx(raw=mint.raw_tx, tx_hash=mint.tx_hash, nonce=int(mint.tx_nonce or 0))
                )
            except Exception as exc:
                if not is_stale_nonce(exc):
                    raise  # RPC trouble: handled by the caller's backoff, mint stays submitted
                # The nonce was consumed by another transaction and ours never mined (no receipt,
                # not minted on-chain). The persisted tx can never land: sign a fresh one.
                log.warning("persisted tx has a consumed nonce; re-queueing")
                await self._requeue(s, mint)
            return False

        # Known but not mined: replace with a higher fee once it has been pending too long.
        age = (datetime.now(UTC) - mint.updated_at).total_seconds()
        if (
            age > self.settings.mint_tx_timeout_seconds
            and mint.attempts < self.settings.mint_max_attempts
        ):
            fn = self._mint_fn(reading, meter)

            async def persist(signed: SignedTx) -> None:
                mint.tx_hash = signed.tx_hash
                mint.raw_tx = signed.raw
                mint.attempts += 1
                await s.commit()

            log.warning("fee-bumping stuck mint", extra={"attempt": mint.attempts + 1})
            try:
                await self.sender.submit(
                    fn,
                    nonce=mint.tx_nonce,
                    fee_bump=FEE_BUMP_STEP**mint.attempts,
                    on_signed=persist,
                )
            except TxRevertedError as exc:
                # Typically ReadingAlreadyMinted: an earlier attempt landed. The next pass adopts it.
                log.info("fee bump skipped: %s", exc)
        return False
