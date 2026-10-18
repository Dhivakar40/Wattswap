"""Chain listener: the single writer of `confirmed`, idempotent and reorg-aware.

* Idempotent: every log is stored in `chain_events` with UNIQUE(tx_hash, log_index). A log is
  applied only when its row is inserted, in the same transaction as its side effects, so replaying
  a range (restart, crash, two instances) changes nothing.
* Final-ish only: logs are applied at depth `listener_confirmations`. The hash of every applied
  block (last `listener_reorg_window`) is stored; each cycle the cursor block's hash is compared
  with the node. On a mismatch the listener finds the common ancestor and ROLLS BACK every event
  above it using the `effects` recorded when it was applied, then re-reads the new canonical chain.
* Reconciliation: periodically the DB's fills/cancels are compared with `exchange.filled` /
  `cancelled` at the cursor block (the block the DB reflects) and any drift is repaired loudly.
"""

import logging
import time
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import redis.asyncio as aioredis
from eth_utils import keccak
from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from web3._utils.events import get_event_data
from web3.exceptions import BlockNotFound
from web3.types import LogReceipt

from app.chain.client import ChainClient
from app.core.config import Settings
from app.core.metrics import LISTENER_LAG, LISTENER_REORGS, SETTLEMENTS
from app.matching import lifecycle
from app.matching.events import Outbox
from app.models import (
    ChainEvent,
    IndexerBlock,
    IndexerState,
    Order,
    OrderStatus,
    Settlement,
    SettlementStatus,
    Trade,
)
from app.schemas.trading import order_out, trade_out

log = logging.getLogger("wattswap.listener")

CURSOR = "exchange"
DIRTY_KEY = "funding:dirty"
TRANSFER_TOPIC = "0x" + keccak(text="Transfer(address,address,uint256)").hex()
APPROVAL_TOPIC = "0x" + keccak(text="Approval(address,address,uint256)").hex()
PARAM_EVENTS = frozenset(
    {"MarketUpdated", "FeeUpdated", "BandUpdated", "TreasuryUpdated", "Paused", "Unpaused"}
)


class ReorgTooDeepError(RuntimeError):
    """The chain reorganised past the stored block-hash window. Needs an operator."""


@dataclass
class ListenerCycle:
    applied: int = 0
    skipped_duplicates: int = 0
    rolled_back_to: int | None = None
    cursor: int | None = None
    head: int = 0
    reconciled: list[str] = field(default_factory=list)


def _hex(v: bytes | bytearray | str) -> str:
    return v.lower() if isinstance(v, str) else "0x" + bytes(v).hex()


class Listener:
    def __init__(
        self,
        settings: Settings,
        sessionmaker: async_sessionmaker[AsyncSession],
        redis: aioredis.Redis,
        client: ChainClient,
    ) -> None:
        self.settings = settings
        self.sessionmaker = sessionmaker
        self.redis = redis
        self.client = client
        self._event_abis: dict[str, dict[str, Any]] = {}
        for item in client.exchange_abi:
            if item.get("type") == "event":
                inputs: list[dict[str, Any]] = list(item.get("inputs", []))  # type: ignore[call-overload]
                sig = f"{item['name']}({','.join(str(i['type']) for i in inputs)})"
                self._event_abis["0x" + keccak(text=sig).hex()] = item
        self._ts_cache: dict[int, int] = {}
        self._last_reconcile = 0.0

    # ------------------------------------------------------------------------- one pass
    async def run_once(self) -> ListenerCycle:
        cfg = self.settings
        cycle = ListenerCycle()
        head = int(await self.client.w3.eth.block_number)
        cycle.head = head
        async with self.sessionmaker() as s:
            state = await s.get(IndexerState, CURSOR)
            cursor = state.last_block if state is not None else None
        if cursor is not None:
            ancestor = await self._find_ancestor(cursor)
            if ancestor != cursor:
                cycle.rolled_back_to = ancestor
                await self._rollback(ancestor)
                cursor = ancestor
        safe = head - cfg.listener_confirmations + 1  # newest block with enough confirmations
        start = (cursor + 1) if cursor is not None else cfg.listener_start_block
        while start <= safe:
            end = min(safe, start + cfg.listener_batch_blocks - 1)
            applied, dups = await self._apply_range(start, end)
            cycle.applied += applied
            cycle.skipped_duplicates += dups
            cursor = end
            start = end + 1
        cycle.cursor = cursor
        LISTENER_LAG.set(max(0, head - (cursor or 0)))
        if time.monotonic() - self._last_reconcile >= cfg.reconcile_seconds and cursor is not None:
            self._last_reconcile = time.monotonic()
            cycle.reconciled = await self.reconcile(cursor)
        return cycle

    # ------------------------------------------------------------------------- reorgs
    async def _node_hash(self, n: int) -> str | None:
        try:
            block = await self.client.w3.eth.get_block(n)
        except BlockNotFound:
            return None
        return _hex(block["hash"])

    async def _find_ancestor(self, cursor: int) -> int:
        """Highest stored block (<= cursor) whose hash the node still agrees with."""
        async with self.sessionmaker() as s:
            stored = {
                b.block_number: b.block_hash
                for b in (
                    await s.scalars(
                        select(IndexerBlock)
                        .where(IndexerBlock.block_number <= cursor)
                        .order_by(IndexerBlock.block_number.desc())
                        .limit(self.settings.listener_reorg_window)
                    )
                )
            }
        if not stored:
            return cursor  # nothing recorded yet (first run): nothing to verify
        for n in sorted(stored, reverse=True):
            if await self._node_hash(n) == stored[n]:
                return n
        raise ReorgTooDeepError(
            f"no stored block hash within {len(stored)} blocks matches the node"
        )

    async def _rollback(self, ancestor: int) -> None:
        out = Outbox()
        async with self.sessionmaker() as s, s.begin():
            events = list(
                await s.scalars(
                    select(ChainEvent)
                    .where(ChainEvent.block_number > ancestor)
                    .order_by(ChainEvent.id.desc())
                )
            )
            for ev in events:
                await self._reverse(s, ev, out)
                await s.delete(ev)
            await s.execute(delete(IndexerBlock).where(IndexerBlock.block_number > ancestor))
            state = await s.get(IndexerState, CURSOR)
            if state is not None:
                state.last_block = ancestor
        LISTENER_REORGS.inc()
        log.warning("chain reorg: rolled back", extra={"to_block": ancestor, "events": len(events)})
        await out.flush(self.redis, self.sessionmaker)

    async def _reverse(self, s: AsyncSession, ev: ChainEvent, out: Outbox) -> None:
        fx = ev.effects or {}
        kind = fx.get("kind")
        if kind == "trade":
            trade = await s.get(Trade, uuid.UUID(fx["trade_id"]))
            if trade is None:
                return
            fill = int(fx["fill"])
            orders = await lifecycle.lock_orders(s, [trade.buy_order_id, trade.sell_order_id])
            to_reserved = fx["prev_status"] in ("submitted", "matched")
            for o in orders.values():
                lifecycle.unapply_fill(o, fill, to_reserved=to_reserved)
                out.order(order_out(o))
            if fx.get("created_trade"):
                st = await s.scalar(select(Settlement).where(Settlement.trade_id == trade.id))
                if st is not None:
                    await s.delete(st)
                await s.delete(trade)
                return
            trade.tx_hash = trade.log_index = trade.block_number = trade.executed_at = None
            st = await s.scalar(select(Settlement).where(Settlement.trade_id == trade.id))
            if st is not None:
                st.status = SettlementStatus(fx["prev_status"])
                st.block_number = None
                SETTLEMENTS.labels("reorged").inc()
        elif kind == "cancel":
            for snap in fx["orders"]:
                row = await s.get(Order, uuid.UUID(snap["id"]))
                if row is not None:
                    row.status = OrderStatus(snap["status"])
                    row.cancel_pending = snap["cancel_pending"]
                    row.cancel_reason = snap["cancel_reason"]
                    row.cancel_type = snap["cancel_type"]
                    out.order(order_out(row))

    # ------------------------------------------------------------------- applying logs
    async def _timestamp(self, block: int) -> int:
        if block not in self._ts_cache:
            self._ts_cache[block] = int((await self.client.w3.eth.get_block(block))["timestamp"])
            if len(self._ts_cache) > 512:
                self._ts_cache.pop(next(iter(self._ts_cache)))
        return self._ts_cache[block]

    async def _apply_range(self, start: int, end: int) -> tuple[int, int]:
        c = self.client
        logs = await c.w3.eth.get_logs(
            {
                "fromBlock": start,
                "toBlock": end,
                "address": [c.exchange_address, c.token_address, c.usdc_address],
            }
        )
        logs = sorted(logs, key=lambda entry: (entry["blockNumber"], entry["logIndex"]))
        out = Outbox()
        applied = dups = 0
        async with self.sessionmaker() as s, s.begin():
            for entry in logs:
                decoded = self._decode(entry)
                if decoded is None:
                    continue
                name, args = decoded
                inserted = (
                    await s.execute(
                        pg_insert(ChainEvent)
                        .values(
                            tx_hash=_hex(entry["transactionHash"]),
                            log_index=int(entry["logIndex"]),
                            block_number=int(entry["blockNumber"]),
                            block_hash=_hex(entry["blockHash"]),
                            address=str(entry["address"]).lower(),
                            name=name,
                            args=args,
                            effects={},
                        )
                        .on_conflict_do_nothing(constraint="uq_chain_events_tx_log")
                        .returning(ChainEvent.id)
                    )
                ).scalar()
                if inserted is None:
                    dups += 1
                    continue  # already applied: idempotent no-op
                effects = await self._apply(s, entry, name, args, out)
                if effects:
                    row = await s.get(ChainEvent, inserted)
                    assert row is not None
                    row.effects = effects
                applied += 1
            first_hash_block = max(start, end - self.settings.listener_reorg_window + 1)
            for n in range(first_hash_block, end + 1):
                h = await self._node_hash(n)
                if h is not None:
                    await s.merge(IndexerBlock(block_number=n, block_hash=h))
            await s.execute(
                delete(IndexerBlock).where(
                    IndexerBlock.block_number < end - self.settings.listener_reorg_window
                )
            )
            state = await s.get(IndexerState, CURSOR)
            if state is None:
                s.add(IndexerState(name=CURSOR, last_block=end))
            else:
                state.last_block = end
        await out.flush(self.redis, self.sessionmaker)
        return applied, dups

    def _decode(self, entry: LogReceipt) -> tuple[str, dict[str, Any]] | None:
        topic0 = _hex(entry["topics"][0]) if entry["topics"] else ""
        addr = str(entry["address"]).lower()
        if addr in (self.client.token_address.lower(), self.client.usdc_address.lower()):
            if topic0 in (TRANSFER_TOPIC, APPROVAL_TOPIC) and len(entry["topics"]) == 3:
                a = "0x" + bytes(entry["topics"][1])[-20:].hex()
                b = "0x" + bytes(entry["topics"][2])[-20:].hex()
                value = int.from_bytes(bytes(entry["data"]), "big")
                key = ("from", "to") if topic0 == TRANSFER_TOPIC else ("owner", "spender")
                name = "Transfer" if topic0 == TRANSFER_TOPIC else "Approval"
                return name, {key[0]: a, key[1]: b, "value": str(value)}
            return None
        abi = self._event_abis.get(topic0)
        if abi is None:
            return None
        data = get_event_data(self.client.w3.codec, abi, entry)
        args = {
            k: (
                _hex(v)
                if isinstance(v, (bytes, bytearray))
                else (str(v).lower() if isinstance(v, str) else str(v))
            )
            for k, v in data["args"].items()
        }
        return str(abi["name"]), args

    async def _apply(
        self, s: AsyncSession, entry: LogReceipt, name: str, args: dict[str, Any], out: Outbox
    ) -> dict[str, Any]:
        if name in ("Transfer", "Approval"):
            for key in ("from", "to", "owner"):
                if key in args and int(args[key], 16) != 0:
                    await self.redis.sadd(DIRTY_KEY, args[key])
            return {}
        if name in PARAM_EVENTS:
            self.client.invalidate_params()
            return {}
        if name == "TradeSettled":
            return await self._on_trade(s, entry, args, out)
        if name == "OrderCancelled":
            order = await s.scalar(select(Order).where(Order.order_hash == args["orderHash"]))
            return await self._cancel_orders(s, [order] if order else [], "on_chain_cancel", out)
        if name == "NonceFloorRaised":
            rows = await s.scalars(
                select(Order).where(
                    Order.maker == args["maker"],
                    Order.status.in_(lifecycle.ACTIVE),
                    Order.nonce < Decimal(args["newMinNonce"]),
                )
            )
            return await self._cancel_orders(s, list(rows), "nonce_floor", out)
        return {}

    async def _cancel_orders(
        self, s: AsyncSession, orders: list[Order], reason: str, out: Outbox
    ) -> dict[str, Any]:
        snaps: list[dict[str, Any]] = []
        for o in orders:
            (locked,) = (await lifecycle.lock_orders(s, [o.id])).values()
            snaps.append(
                {
                    "id": str(locked.id),
                    "status": locked.status.value,
                    "cancel_pending": locked.cancel_pending,
                    "cancel_reason": locked.cancel_reason,
                    "cancel_type": locked.cancel_type,
                }
            )
            await lifecycle.cancel_order(s, locked, reason, "onchain", out, fail_inflight=True)
        return {"kind": "cancel", "orders": snaps} if snaps else {}

    async def _on_trade(
        self, s: AsyncSession, entry: LogReceipt, args: dict[str, Any], out: Outbox
    ) -> dict[str, Any]:
        tx_hash = _hex(entry["transactionHash"])
        block = int(entry["blockNumber"])
        fill = int(args["fillWh"])
        buy_hash, sell_hash = args["buyHash"], args["sellHash"]
        ts = datetime.fromtimestamp(await self._timestamp(block), UTC)

        candidates = list(
            await s.scalars(
                select(Settlement)
                .where(Settlement.buy_hash == buy_hash, Settlement.sell_hash == sell_hash)
                .order_by(Settlement.created_at)
            )
        )
        st: Settlement | None = next(
            (c for c in candidates if tx_hash in (c.tx_hashes or [])), None
        )
        if (
            st is None
        ):  # settled by a tx we did not record: adopt an in-flight settlement of the pair
            for c in candidates:
                t = await s.get(Trade, c.trade_id)
                if (
                    t is not None
                    and int(t.fill_wh) == fill
                    and c.status in (SettlementStatus.MATCHED, SettlementStatus.SUBMITTED)
                ):
                    st = c
                    break

        if st is not None:
            trade = await s.get(Trade, st.trade_id)
            assert trade is not None
            prev = st.status
            orders = await lifecycle.lock_orders(s, [trade.buy_order_id, trade.sell_order_id])
            from_reserved = prev in lifecycle.IN_FLIGHT
            for o in orders.values():
                if not from_reserved:
                    await lifecycle.make_room(s, o, fill, out)
                lifecycle.apply_fill(o, fill, from_reserved=from_reserved)
                out.order(order_out(o))
            st.status = SettlementStatus.CONFIRMED
            st.exec_price = Decimal(args["execPrice"])
            st.block_number = block
            st.tx_hash = tx_hash
            st.failure_reason = None
            self._fill_trade(trade, tx_hash, entry, block, ts, args)
            await lifecycle.clear_cooldown(s, buy_hash, sell_hash)
            SETTLEMENTS.labels("confirmed").inc()
            await s.flush()
            out.trade(trade_out(trade, st))
            return {
                "kind": "trade",
                "trade_id": str(trade.id),
                "prev_status": prev.value,
                "fill": fill,
                "created_trade": False,
            }

        # Not one of ours: record it and move the orders we know about.
        buy = await s.scalar(select(Order).where(Order.order_hash == buy_hash))
        sell = await s.scalar(select(Order).where(Order.order_hash == sell_hash))
        if buy is None or sell is None:
            log.warning("TradeSettled for unknown orders ignored", extra={"tx": tx_hash})
            return {}
        log.warning("external settlement adopted", extra={"tx": tx_hash})
        trade = Trade(
            id=uuid.uuid4(),
            market_id=args["marketId"],
            buy_order_id=buy.id,
            sell_order_id=sell.id,
            buyer=args["buyer"],
            seller=args["seller"],
            fill_wh=Decimal(fill),
            exec_price=Decimal(args["execPrice"]),
            cost=Decimal(args["cost"]),
            fee=Decimal(args["fee"]),
            created_at=ts,
            external=True,
        )
        self._fill_trade(trade, tx_hash, entry, block, ts, args)
        s.add(trade)
        orders = await lifecycle.lock_orders(s, [buy.id, sell.id])
        for o in orders.values():
            await lifecycle.make_room(s, o, fill, out)
            lifecycle.apply_fill(o, fill, from_reserved=False)
            out.order(order_out(o))
        await s.flush()
        out.trade(trade_out(trade, None))
        return {
            "kind": "trade",
            "trade_id": str(trade.id),
            "prev_status": "failed",
            "fill": fill,
            "created_trade": True,
        }

    @staticmethod
    def _fill_trade(
        trade: Trade,
        tx_hash: str,
        entry: LogReceipt,
        block: int,
        ts: datetime,
        args: dict[str, Any],
    ) -> None:
        trade.tx_hash = tx_hash
        trade.log_index = int(entry["logIndex"])
        trade.block_number = block
        trade.executed_at = ts
        trade.exec_price = Decimal(args["execPrice"])  # the chain's numbers win
        trade.cost = Decimal(args["cost"])
        trade.fee = Decimal(args["fee"])

    # ---------------------------------------------------------------- reconciliation
    async def reconcile(self, block: int) -> list[str]:
        """Compare DB fills/cancels with the chain AT THE CURSOR BLOCK; repair any drift."""
        out = Outbox()
        fixed: list[str] = []
        c = self.client
        async with self.sessionmaker() as s, s.begin():
            orders = list(
                await s.scalars(
                    select(Order)
                    .where(Order.status.in_(lifecycle.ACTIVE))
                    .order_by(Order.id)
                    .limit(500)
                )
            )
            for o in orders:
                at: Any = block
                chain_filled = int(
                    await c.exchange.functions.filled(o.order_hash).call(block_identifier=at)
                )
                chain_cancelled = bool(
                    await c.exchange.functions.cancelled(o.order_hash).call(block_identifier=at)
                )
                (locked,) = (await lifecycle.lock_orders(s, [o.id])).values()
                if chain_cancelled and locked.status in lifecycle.ACTIVE:
                    log.error(
                        "drift: order cancelled on-chain but active in DB",
                        extra={"order": o.order_hash},
                    )
                    await lifecycle.cancel_order(
                        s, locked, "on_chain_cancel", "onchain", out, fail_inflight=True
                    )
                    fixed.append(f"cancelled:{o.order_hash}")
                if chain_filled != lifecycle.filled(locked):
                    log.error(
                        "drift: fills differ from chain; repairing",
                        extra={
                            "order": o.order_hash,
                            "db": lifecycle.filled(locked),
                            "chain": chain_filled,
                        },
                    )
                    await lifecycle.make_room(
                        s, locked, chain_filled - lifecycle.filled(locked), out
                    )
                    locked.filled_wh = Decimal(chain_filled)
                    if locked.status in lifecycle.ACTIVE:
                        lifecycle._set_status_from_fills(locked)
                    out.order(order_out(locked))
                    fixed.append(f"filled:{o.order_hash}")
        await out.flush(self.redis, self.sessionmaker)
        return fixed
