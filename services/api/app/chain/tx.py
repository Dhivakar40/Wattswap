"""Transaction sender: nonce management, gas/fee estimation, fee bumping, confirmations."""

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

import redis.asyncio as aioredis
from eth_account.signers.local import LocalAccount
from eth_typing import HexStr
from eth_utils import to_checksum_address
from web3.contract.async_contract import AsyncContractFunction
from web3.exceptions import ContractLogicError, TransactionNotFound
from web3.types import TxReceipt

from app.chain.client import ChainClient
from app.chain.revert import RevertInfo

log = logging.getLogger("wattswap.chain.tx")

GAS_MARGIN = 1.25


@dataclass(frozen=True)
class SignedTx:
    raw: str  # 0x-hex signed transaction
    tx_hash: str
    nonce: int


class TxRevertedError(Exception):
    """A simulated or mined transaction reverted. `info` is the decoded custom error, if any."""

    def __init__(self, info: RevertInfo | None, message: str) -> None:
        super().__init__(message)
        self.info = info


OnSigned = Callable[[SignedTx], Awaitable[None]]


class TxSender:
    """Signs and sends transactions for one role key. A Redis lock serialises nonce use across
    API instances, so two processes holding the same key never race for a nonce."""

    def __init__(
        self, client: ChainClient, account: LocalAccount, redis: aioredis.Redis, name: str
    ) -> None:
        self.client = client
        self.account = account
        self.redis = redis
        self.name = name

    @property
    def address(self) -> str:
        return str(self.account.address)

    async def _fees(self, bump: float) -> dict[str, int]:
        w3 = self.client.w3
        block = await w3.eth.get_block("latest")
        base = int(block.get("baseFeePerGas", 0))
        prio = int(await w3.eth.max_priority_fee)
        prio_b = int(prio * bump) + (1 if bump > 1 else 0)
        return {
            "maxPriorityFeePerGas": prio_b,
            "maxFeePerGas": int((base * 2 + prio) * bump) + prio_b,
        }

    async def simulate(self, fn: AsyncContractFunction) -> int:
        """Gas estimate; doubles as a dry run (raises TxRevertedError with the decoded error)."""
        try:
            return int(await fn.estimate_gas({"from": self.address}))
        except ContractLogicError as exc:
            info = self.client.revert_from_exception(exc)
            raise TxRevertedError(info, str(info) if info else str(exc)) from exc

    async def submit(
        self,
        fn: AsyncContractFunction,
        *,
        nonce: int | None = None,
        fee_bump: float = 1.0,
        on_signed: OnSigned | None = None,
    ) -> SignedTx:
        """Estimate, sign, hand the signed tx to `on_signed` (to persist it), then broadcast."""
        async with self.redis.lock(f"lock:tx:{self.name}", timeout=60, blocking_timeout=60):
            gas = await self.simulate(fn)
            if nonce is None:
                nonce = int(
                    await self.client.w3.eth.get_transaction_count(
                        to_checksum_address(self.address), "pending"
                    )
                )
            params: dict[str, Any] = {
                "from": self.address,
                "nonce": nonce,
                "gas": int(gas * GAS_MARGIN),
                "chainId": self.client.settings.chain_id,
                **await self._fees(fee_bump),
            }
            tx = await fn.build_transaction(params)  # type: ignore[arg-type]
            signed = self.account.sign_transaction(tx)  # type: ignore[arg-type]
            raw = "0x" + signed.raw_transaction.hex()
            result = SignedTx(raw=raw, tx_hash="0x" + signed.hash.hex(), nonce=nonce)
            if on_signed is not None:
                await on_signed(result)
            await self.broadcast(result)
            return result

    async def broadcast(self, signed: SignedTx) -> None:
        """Send a signed tx. Re-sending an already-known tx is harmless."""
        try:
            await self.client.w3.eth.send_raw_transaction(HexStr(signed.raw))
        except Exception as exc:
            text = str(exc).lower()
            if "already known" in text or "already imported" in text or "known transaction" in text:
                return
            raise

    async def receipt(self, tx_hash: str) -> TxReceipt | None:
        try:
            return await self.client.w3.eth.get_transaction_receipt(tx_hash)  # type: ignore[arg-type]
        except TransactionNotFound:
            return None

    async def wait(
        self, tx_hashes: list[str], timeout_s: float, confirmations: int = 1
    ) -> TxReceipt | None:
        """Wait for any of `tx_hashes` (original + fee-bumped replacements) to confirm."""
        deadline = time.monotonic() + timeout_s
        w3 = self.client.w3
        while True:
            for h in tx_hashes:
                rcpt = await self.receipt(h)
                if rcpt is not None:
                    head = int(await w3.eth.block_number)
                    if head >= int(rcpt["blockNumber"]) + confirmations - 1:
                        return rcpt
            if time.monotonic() >= deadline:
                return None
            await asyncio.sleep(0.25)

    async def revert_reason(self, tx_hash: str) -> RevertInfo | None:
        """Replay a mined, failed transaction one block earlier to recover its revert data."""
        w3 = self.client.w3
        tx = await w3.eth.get_transaction(tx_hash)  # type: ignore[arg-type]
        call: dict[str, Any] = {
            "from": tx["from"],
            "to": tx["to"],
            "data": tx["input"],
            "value": tx.get("value", 0),
        }
        try:
            await w3.eth.call(call, block_identifier=int(tx["blockNumber"]) - 1)  # type: ignore[arg-type]
        except ContractLogicError as exc:
            return self.client.revert_from_exception(exc)
        return None

    async def send_and_wait(
        self, fn: AsyncContractFunction, timeout_s: float, confirmations: int = 1
    ) -> TxReceipt:
        """Fire-and-confirm for low-volume admin transactions (meter registration)."""
        signed = await self.submit(fn)
        rcpt = await self.wait([signed.tx_hash], timeout_s, confirmations)
        if rcpt is None:
            raise TimeoutError(f"transaction {signed.tx_hash} not confirmed in {timeout_s}s")
        if int(rcpt["status"]) != 1:
            info = await self.revert_reason(signed.tx_hash)
            raise TxRevertedError(
                info, str(info) if info else f"transaction {signed.tx_hash} reverted"
            )
        return rcpt
