"""Settlement relayer: matched -> submitted (and detecting failures). The listener confirms.

Per settlement, immediately before anything is signed:
  1. pre-flight: orders still active and not about to expire, voided/unfunded orders cancelled,
     no overfill against the chain;
  2. FRESH quote: the latest signed engine quote must have at least `quote_safety_margin` seconds of
     validity left, because the tx is mined later than it is signed;
  3. the execution price is re-derived from that quote (the matcher's price is provisional); with no
     price that satisfies limits AND the quote band the pair is RELEASED with exponential backoff;
  4. dry-run (`estimate_gas`), sign, persist the raw tx, broadcast.
A stuck tx is replaced at the SAME nonce with a higher fee and a fresh quote, so a quote can never
age out while a transaction waits in the mempool. A mined revert is classified (settle_errors) and
either retried (quote trouble), or fails the settlement and removes the offending order (cancel
race, unfunded, ...) without retrying.
"""

import logging
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import timedelta
from decimal import Decimal
from typing import Any

import redis.asyncio as aioredis
from eth_utils import to_checksum_address
from hexbytes import HexBytes
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from web3.contract.async_contract import AsyncContractFunction

from app.chain.client import ChainClient
from app.chain.revert import RevertInfo
from app.chain.settle_errors import ALARMING, Verdict, classify
from app.chain.tx import SignedTx, TxRevertedError, TxSender
from app.core.config import Settings
from app.core.metrics import (
    INVARIANT_VIOLATIONS,
    RELAYER_IN_FLIGHT,
    SETTLEMENT_RELEASES,
    SETTLEMENTS,
)
from app.matching import lifecycle, snapshot
from app.matching.events import Outbox
from app.matching.matcher import PAUSE_KEY, Quote, load_quote
from app.matching.rules import feasible_price, fill_is_worth_settling
from app.models import Order, Settlement, SettlementStatus, Trade

log = logging.getLogger("wattswap.relayer")

FEE_BUMP_STEP = 1.25
MAX_BUMPS = 8
HALT_KEY = "trading:relayer_halt"


@dataclass
class Prepared:
    fn: AsyncContractFunction
    exec_price: int
    quote: Quote


@dataclass
class RelayerCycle:
    submitted: int = 0
    bumped: int = 0
    released: int = 0
    deferred: int = 0
    failed: int = 0
    notes: list[str] = field(default_factory=list)


def order_tuple(o: Order) -> tuple[Any, ...]:
    return (
        to_checksum_address(o.maker),
        HexBytes(o.market_id),
        0 if o.side.value == "buy" else 1,
        int(o.amount_wh),
        int(o.limit_price),
        int(o.expiry),
        int(o.nonce),
        HexBytes(o.salt),
    )


class Relayer:
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
        # Test seam: called after the signed tx is persisted and BEFORE it is broadcast.
        self.before_broadcast: Callable[[Settlement], Awaitable[None]] | None = None
        self._infra: dict[uuid.UUID, tuple[int, float]] = {}

    # ----------------------------------------------------------------------------- loop
    async def run_once(self) -> RelayerCycle:
        cycle = RelayerCycle()
        async with self.sessionmaker() as s:
            submitted = list(
                (
                    await s.scalars(
                        select(Settlement.id).where(Settlement.status == SettlementStatus.SUBMITTED)
                    )
                ).all()
            )
        RELAYER_IN_FLIGHT.set(len(submitted))
        for sid in submitted:
            await self._check_submitted(sid, cycle)

        if await self.redis.get(PAUSE_KEY) or await self.redis.get(HALT_KEY):
            cycle.notes.append("halted")
            return cycle
        capacity = self.settings.settle_max_in_flight - len(submitted)
        if capacity <= 0:
            return cycle
        async with self.sessionmaker() as s:
            queued = list(
                (
                    await s.scalars(
                        select(Settlement.id)
                        .where(
                            Settlement.status == SettlementStatus.MATCHED,
                            (Settlement.next_attempt_at.is_(None))
                            | (Settlement.next_attempt_at <= lifecycle.now()),
                        )
                        .order_by(Settlement.created_at)
                        .limit(capacity)
                    )
                ).all()
            )
        for sid in queued:
            if time.monotonic() < self._infra.get(sid, (0, 0.0))[1]:
                continue
            await self._submit(sid, cycle)
        return cycle

    # --------------------------------------------------------------------- submit path
    async def _submit(self, sid: uuid.UUID, cycle: RelayerCycle) -> None:
        out = Outbox()
        async with self.sessionmaker() as s:
            st = await s.scalar(select(Settlement).where(Settlement.id == sid).with_for_update())
            if st is None or st.status != SettlementStatus.MATCHED:
                return
            trade = await s.get(Trade, st.trade_id)
            assert trade is not None
            orders = await lifecycle.lock_orders(s, [trade.buy_order_id, trade.sell_order_id])
            buy, sell = orders[trade.buy_order_id], orders[trade.sell_order_id]
            try:
                prepared, why = await self._prepare(s, st, trade, buy, sell, out, resubmit=False)
                if prepared is None:
                    await s.commit()
                    self._count(cycle, why)
                    return
                await self._send(s, st, prepared, nonce=None, bump=1.0)
                cycle.submitted += 1
                SETTLEMENTS.labels("submitted").inc()
            except TxRevertedError as exc:
                await self._apply_revert(s, st, buy, sell, exc.info, out, mined=False)
                await s.commit()
                cycle.failed += 1
            except Exception as exc:
                await s.rollback()
                self._defer(sid, f"{type(exc).__name__}: {exc}")
                cycle.deferred += 1
        await out.flush(self.redis, self.sessionmaker)

    def _count(self, cycle: RelayerCycle, why: str) -> None:
        if why == "released":
            cycle.released += 1
        elif why == "deferred":
            cycle.deferred += 1
        else:
            cycle.failed += 1
        cycle.notes.append(why)

    def _defer(self, sid: uuid.UUID, why: str) -> None:
        """Infrastructure trouble (RPC, nonce, gas): back off and retry; never fail the trade."""
        failures = self._infra.get(sid, (0, 0.0))[0] + 1
        cfg = self.settings
        delay = min(
            cfg.settle_backoff_max_seconds, cfg.settle_backoff_base_seconds * 2 ** (failures - 1)
        )
        self._infra[sid] = (failures, time.monotonic() + delay)
        log.warning(
            "settlement deferred", extra={"settlement": str(sid), "why": why, "retry_in_s": delay}
        )

    async def _prepare(
        self,
        s: AsyncSession,
        st: Settlement,
        trade: Trade,
        buy: Order,
        sell: Order,
        out: Outbox,
        *,
        resubmit: bool,
    ) -> tuple[Prepared | None, str]:
        """Pre-flight + fresh quote + price + contract call. Returns (prepared, why-not)."""
        cfg = self.settings
        fill = int(trade.fill_wh)
        chain_now = await self.client.latest_block_timestamp()
        params = await self.client.exchange_params()

        for o in (buy, sell):
            if o.status not in lifecycle.ACTIVE:
                await lifecycle.fail_settlement(
                    s, st, f"preflight:order_inactive:{o.order_hash}", out
                )
                return None, "failed"
            if o.expiry <= chain_now + cfg.expiry_margin_seconds:
                await lifecycle.fail_settlement(s, st, f"preflight:expired:{o.order_hash}", out)
                return None, "failed"

        # Voided on-chain or unfunded right now? (cancels them and fails this settlement)
        evals = {}
        for maker in sorted({buy.maker, sell.maker}):
            _, ev = await snapshot.enforce(s, self.client, maker, out, chain_now)
            evals[maker] = ev
        await s.refresh(st)
        if st.status not in lifecycle.IN_FLIGHT:
            return None, "failed"
        for o in (buy, sell):
            ev = evals.get(o.maker)
            if ev is not None and ev.snapshot.filled.get(o.order_hash, 0) + fill > int(o.amount_wh):
                await self._release(s, st, buy, sell, "overfill_chain_ahead", out)
                return None, "released"

        quote = await load_quote(self.redis, trade.market_id)
        if quote is None or not quote.signature:
            return None, "deferred"
        if not (
            quote.timestamp <= chain_now
            and chain_now - quote.timestamp
            <= params.max_staleness - cfg.quote_safety_margin_seconds
        ):
            return None, "deferred"  # the pricing engine signs a new one every tick

        price = feasible_price(
            quote.price,
            int(sell.limit_price),
            int(buy.limit_price),
            quote.price,
            params.band_bps,
            cfg.band_safety_pct,
        )
        if price is None or not fill_is_worth_settling(fill, price, cfg.min_fill_cost):
            if resubmit:
                return None, "deferred"  # keep the tx already in flight; do not fail it
            await self._release(s, st, buy, sell, "price_band", out)
            return None, "released"

        fn = self.client.exchange.functions.settle(
            order_tuple(buy),
            bytes(HexBytes(buy.signature)),
            order_tuple(sell),
            bytes(HexBytes(sell.signature)),
            fill,
            price,
            (HexBytes(trade.market_id), quote.price, quote.timestamp),
            bytes(HexBytes(quote.signature)),
        )
        return Prepared(fn, price, quote), "ok"

    async def _release(
        self, s: AsyncSession, st: Settlement, buy: Order, sell: Order, reason: str, out: Outbox
    ) -> None:
        """Return a pair to the book, on an exponentially growing cooldown (no re-match loop)."""
        cfg = self.settings
        delay = await lifecycle.record_release(
            s,
            buy.order_hash,
            sell.order_hash,
            reason,
            cfg.pair_cooldown_seconds,
            cfg.pair_cooldown_max_seconds,
        )
        await lifecycle.fail_settlement(s, st, f"released:{reason}", out)
        SETTLEMENT_RELEASES.labels(reason).inc()
        log.info(
            "pair released back to the book",
            extra={
                "buy": buy.order_hash,
                "sell": sell.order_hash,
                "reason": reason,
                "cooldown_s": delay,
            },
        )

    async def _send(
        self, s: AsyncSession, st: Settlement, p: Prepared, *, nonce: int | None, bump: float
    ) -> SignedTx:
        """Sign, persist (raw tx first!), broadcast."""
        fresh = st.status == SettlementStatus.MATCHED

        async def on_signed(signed: SignedTx) -> None:
            st.exec_price = Decimal(p.exec_price)
            st.quote_price = Decimal(p.quote.price)
            st.quote_timestamp = p.quote.timestamp
            st.raw_tx = signed.raw
            st.tx_nonce = signed.nonce
            st.tx_hash = signed.tx_hash
            st.tx_hashes = [signed.tx_hash] if fresh else [*st.tx_hashes, signed.tx_hash]
            st.submitted_at = lifecycle.now()
            st.status = SettlementStatus.SUBMITTED
            st.next_attempt_at = None
            await s.commit()  # durable BEFORE the broadcast: a crash re-sends this same tx
            if self.before_broadcast is not None:
                await self.before_broadcast(st)

        return await self.sender.submit(p.fn, nonce=nonce, fee_bump=bump, on_signed=on_signed)

    # ------------------------------------------------------------------ in-flight txs
    async def _check_submitted(self, sid: uuid.UUID, cycle: RelayerCycle) -> None:
        out = Outbox()
        async with self.sessionmaker() as s:
            st = await s.scalar(select(Settlement).where(Settlement.id == sid).with_for_update())
            if st is None or st.status != SettlementStatus.SUBMITTED:
                return  # e.g. the listener already failed it (cancel race seen on-chain)
            trade = await s.get(Trade, st.trade_id)
            assert trade is not None
            orders = await lifecycle.lock_orders(s, [trade.buy_order_id, trade.sell_order_id])
            buy, sell = orders[trade.buy_order_id], orders[trade.sell_order_id]
            try:
                await self._advance(s, st, trade, buy, sell, out, cycle)
            except TxRevertedError as exc:
                await self._apply_revert(s, st, buy, sell, exc.info, out, mined=False)
                await s.commit()
            except Exception as exc:
                await s.rollback()
                log.warning("checking settlement failed: %s", exc, extra={"settlement": str(sid)})
        await out.flush(self.redis, self.sessionmaker)

    async def _advance(
        self,
        s: AsyncSession,
        st: Settlement,
        trade: Trade,
        buy: Order,
        sell: Order,
        out: Outbox,
        cycle: RelayerCycle,
    ) -> None:
        cfg = self.settings
        for h in st.tx_hashes:
            receipt = await self.sender.receipt(h)
            if receipt is None:
                continue
            if int(receipt["status"]) == 1:
                if st.block_number is None:  # the listener confirms from the event, at depth
                    st.block_number = int(receipt["blockNumber"])
                    st.tx_hash = h
                await s.commit()
                return
            info = await self.sender.revert_reason(h)
            if info is None:
                info = await self._infer_revert(st, int(receipt["blockNumber"]))
            await self._apply_revert(s, st, buy, sell, info, out, mined=True)
            await s.commit()
            cycle.failed += 1
            return

        assert st.tx_nonce is not None and st.raw_tx is not None
        w3 = self.client.w3
        addr = to_checksum_address(self.sender.address)
        if int(await w3.eth.get_transaction_count(addr, "latest")) > st.tx_nonce:
            # The nonce was consumed by a transaction that is not one of ours: our tx is gone.
            log.warning("settlement tx dropped; re-queueing", extra={"settlement": str(st.id)})
            self._requeue(st, "tx_dropped")
            await s.commit()
            return
        known = False
        for h in st.tx_hashes:
            try:
                known = known or (await w3.eth.get_transaction(h)) is not None  # type: ignore[arg-type]
            except Exception as exc:  # TransactionNotFound: the node does not know it
                log.debug("tx %s not known to the node: %s", h, exc)
        if not known:  # lost in a crash or by the node: re-send the SAME signed tx
            await self.sender.broadcast(SignedTx(st.raw_tx, st.tx_hashes[-1], st.tx_nonce))
            await s.commit()
            return

        chain_now = await self.client.latest_block_timestamp()
        params = await self.client.exchange_params()
        age = (lifecycle.now() - (st.submitted_at or lifecycle.now())).total_seconds()
        quote_age = chain_now - (st.quote_timestamp or chain_now)
        quote_unsafe = quote_age >= params.max_staleness - cfg.quote_safety_margin_seconds
        if (age < cfg.settle_bump_after_seconds and not quote_unsafe) or len(
            st.tx_hashes
        ) > MAX_BUMPS:
            await s.commit()
            return
        # Replace at the same nonce: higher fee AND a fresh quote (and a re-derived price).
        prepared, why = await self._prepare(s, st, trade, buy, sell, out, resubmit=True)
        if prepared is None:
            await s.commit()
            cycle.notes.append(f"bump_skipped:{why}")
            return
        bump = FEE_BUMP_STEP ** len(st.tx_hashes)
        await self._send(s, st, prepared, nonce=st.tx_nonce, bump=bump)
        cycle.bumped += 1

    async def _infer_revert(self, st: Settlement, block_number: int) -> RevertInfo | None:
        """A replay one block earlier cannot see the block's own timestamp. If the quote was
        already too old by the time of the block that mined the tx, that is the reason."""
        block = await self.client.w3.eth.get_block(block_number)
        params = await self.client.exchange_params()
        if (
            st.quote_timestamp is not None
            and int(block["timestamp"]) - st.quote_timestamp > params.max_staleness
        ):
            return RevertInfo("StaleQuote", {"inferred": True})
        return None

    def _requeue(self, st: Settlement, reason: str) -> None:
        cfg = self.settings
        st.status = SettlementStatus.MATCHED
        st.attempts += 1
        st.raw_tx = None
        st.tx_nonce = None
        st.block_number = None
        st.next_attempt_at = lifecycle.now() + timedelta(
            seconds=min(
                cfg.settle_backoff_max_seconds, cfg.settle_backoff_base_seconds * 2**st.attempts
            )
        )
        st.failure_reason = f"retry:{reason}"

    # -------------------------------------------------------------------- revert policy
    async def _apply_revert(
        self,
        s: AsyncSession,
        st: Settlement,
        buy: Order,
        sell: Order,
        info: RevertInfo | None,
        out: Outbox,
        *,
        mined: bool,
    ) -> None:
        cfg = self.settings
        verdict = classify(info)
        label = str(info) if info else "unknown"
        log.warning(
            "settle reverted",
            extra={
                "settlement": str(st.id),
                "verdict": verdict.value,
                "revert": label,
                "mined": mined,
            },
        )
        if verdict in (Verdict.CANCEL_RACE, Verdict.ORDER_BAD, Verdict.UNDERFUNDED):
            # Re-read both makers on-chain to find who is at fault; this cancels the culprit(s)
            # (with the right cancel_type) and fails this settlement, releasing the counterpart.
            chain_now = await self.client.latest_block_timestamp()
            for maker in sorted({buy.maker, sell.maker}):
                await snapshot.enforce(s, self.client, maker, out, chain_now)
            await s.refresh(st)
            if st.status in lifecycle.IN_FLIGHT:  # nobody provably at fault (expiry, signature...)
                await self._release(s, st, buy, sell, f"revert_{verdict.value}", out)
            return
        if verdict is Verdict.BUG:
            INVARIANT_VIOLATIONS.inc()
            log.error("matcher produced a pair the exchange rejected", extra={"revert": label})
            await self._release(s, st, buy, sell, f"matcher_bug:{label}", out)
            return
        if verdict is Verdict.SYSTEMIC:
            log.error(
                "exchange refuses settlement; halting relayer for 60s", extra={"revert": label}
            )
            await self.redis.set(HALT_KEY, label, ex=60)
            self._requeue(st, f"systemic:{label}")
            st.attempts -= 1  # not the pair's fault
            return
        if verdict is Verdict.UNKNOWN and mined:
            # Replaying one block earlier cannot see effects earlier in the SAME block (an approval
            # revoked, an order cancelled): diagnose from the chain's current state instead.
            chain_now = await self.client.latest_block_timestamp()
            for maker in sorted({buy.maker, sell.maker}):
                await snapshot.enforce(s, self.client, maker, out, chain_now)
            await s.refresh(st)
            if st.status not in lifecycle.IN_FLIGHT:
                return
        # TRANSIENT (stale/future quote, outside band) or UNKNOWN: bounded retry.
        if info is not None and info.name in ALARMING:
            log.error("quote signer is not authorised on the oracle", extra={"revert": label})
        if st.attempts + 1 >= cfg.settle_max_attempts:
            await self._release(s, st, buy, sell, f"gave_up:{label}", out)
            return
        self._requeue(st, label)
