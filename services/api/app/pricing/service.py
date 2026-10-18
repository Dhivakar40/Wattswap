"""The pricing service: wires the pure engine to weather, the database, Redis and the chain.

Runs as three cooperating loops (weather poll, ticker, oracle pusher) under one Redis leader
lock, so any number of API instances (or a standalone `python -m app.pricing.run`) can be
started and exactly one prices. All state needed after a restart is rebuilt from the last
persisted ticks.
"""

import asyncio
import contextlib
import json
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import redis.asyncio as aioredis
from redis.exceptions import LockError
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.chain.client import ChainClient
from app.chain.tx import TxSender
from app.core.config import Settings
from app.core.metrics import (
    BREAKER_STATE,
    ORACLE_PUSHES,
    PRICE,
    PRICE_RAW,
    PRICE_VOL,
    PRICING_EVENTS,
    PRICING_TICKS,
    WEATHER_AGE,
)
from app.models import Market, Meter, Mint, MintStatus, OrderSide, Reading, User
from app.pricing import publish, repo
from app.pricing.breaker import Mode
from app.pricing.engine import EngineState, Tick, forecast_row, reset_breaker, restore_state, step
from app.pricing.model import CellInput, MarketInputs
from app.pricing.params import PricingParams
from app.pricing.quotes import PriceQuote, SignedQuote, sign_quote
from app.pricing.repo import MarketCell
from app.pricing.weather import Lookup, WeatherService

log = logging.getLogger("wattswap.pricing")

LEADER_LOCK = "lock:pricing"
BREAKER_GAUGE = {Mode.NORMAL: 0, Mode.ANOMALY: 1, Mode.FROZEN: 2, Mode.RECOVERING: 3}
FORECAST_EVERY_S = 300.0
PARAMS_TTL_S = 5.0
BAND_TTL_S = 60.0


class ChainClock:
    """Timestamps for quotes. Wall time, but never ahead of the latest block while the chain is
    producing blocks (the exchange rejects quotes from the future). On an idle dev chain whose
    last block is old, the next block will carry the current wall time, so wall time is used."""

    FRESH_S = 60

    def __init__(self, client: ChainClient | None, wall: Callable[[], float]) -> None:
        self._client = client
        self._wall = wall

    async def quote_timestamp(self) -> int:
        wall = int(self._wall())
        if self._client is None:
            return wall
        try:
            block_ts = await self._client.latest_block_timestamp()
        except Exception as exc:
            log.warning("cannot read the latest block (%s); using wall clock for the quote", exc)
            return wall
        return min(wall, block_ts) if wall - block_ts <= self.FRESH_S else wall


@dataclass
class OracleStatus:
    last_price: int | None = None
    last_wall: float = 0.0
    last_onchain_ts: int = 0
    next_try: float = 0.0
    failures: int = 0
    last_result: str = "never"
    last_tx: str | None = None


@dataclass
class MarketRuntime:
    state: EngineState = field(default_factory=EngineState)
    restored: bool = False
    params: PricingParams | None = None
    params_at: float = 0.0
    band: int = 500
    band_at: float = -1e9
    forecast_at: float = -1e9
    latest: Tick | None = None
    latest_quote: SignedQuote | None = None
    oracle: OracleStatus = field(default_factory=OracleStatus)


class PricingService:
    def __init__(
        self,
        settings: Settings,
        sessionmaker: async_sessionmaker[AsyncSession],
        redis: aioredis.Redis,
        weather: WeatherService,
        *,
        chain: ChainClient | None = None,
        pricer: TxSender | None = None,
        wall: Callable[[], float] = time.time,
    ) -> None:
        self.settings = settings
        self.sessionmaker = sessionmaker
        self.redis = redis
        self.weather = weather
        self.chain = chain
        self.pricer = pricer
        self._wall = wall
        self.clock = ChainClock(chain, wall)
        self.markets: dict[str, MarketRuntime] = {}
        self._balances: dict[str, tuple[int, float]] = {}  # owner -> (Wh, monotonic fetched)
        pricer_key = settings.pricer_private_key
        self._pricer_key = None if pricer_key is None else pricer_key.get_secret_value()

    # ------------------------------------------------------------------ helpers
    def _runtime(self, market_id: str) -> MarketRuntime:
        return self.markets.setdefault(market_id, MarketRuntime())

    async def _params(self, s: AsyncSession, market_id: str, rt: MarketRuntime) -> PricingParams:
        now = self._wall()
        if rt.params is None or now - rt.params_at > PARAMS_TTL_S:
            rt.params = await repo.effective_params(
                s, market_id, self.settings.pricing_tick_seconds
            )
            rt.params_at = now
        return rt.params

    async def _onchain_band(self, rt: MarketRuntime) -> int:
        if self.chain is None:
            return rt.band
        now = self._wall()
        if now - rt.band_at > BAND_TTL_S:
            try:
                rt.band = await self.chain.onchain_band_bps()
                rt.band_at = now
            except Exception as exc:
                log.warning("cannot read the exchange band (%s); keeping %d bps", exc, rt.band)
        return rt.band

    async def _minted_wh(self, s: AsyncSession, owners: set[str]) -> int:
        """Fallback when the chain is unreachable: WATT minted to these owners (ignores transfers)."""
        if not owners:
            return 0
        total = await s.scalar(
            select(func.coalesce(func.sum(Reading.amount_wh), 0))
            .join(Mint, Mint.reading_pk == Reading.id)
            .join(Meter, Meter.id == Reading.meter_pk)
            .join(User, User.id == Meter.owner_id)
            .where(Mint.status == MintStatus.CONFIRMED, User.address.in_(owners))
        )
        return int(total or 0)

    async def _inventory(
        self, s: AsyncSession, market_id: str, owners: set[str], now_ts: int
    ) -> tuple[int, str]:
        """Tradeable inventory: open sell volume once the book exists, otherwise the WATT held by
        the owners of the market's active meters (read from the chain, cached briefly)."""
        mode = self.settings.pricing_inventory_source
        if mode in ("auto", "open_sells"):
            sells = await repo.open_order_wh(s, market_id, OrderSide.SELL, now_ts)
            if sells > 0 or mode == "open_sells":
                return sells, "open_sells"
        if self.chain is None:
            return await self._minted_wh(s, owners), "db_minted_fallback"
        total = 0
        for owner in sorted(owners):
            cached = self._balances.get(owner)
            fresh = cached is not None and (
                self._wall() - cached[1] < self.settings.pricing_inventory_refresh_seconds
            )
            if not fresh:
                try:
                    self._balances[owner] = (await self.chain.balance_of(owner), self._wall())
                except Exception as exc:
                    log.warning("balanceOf(%s) failed (%s)", owner, exc)
                    if cached is None:  # no value at all: fall back for the whole market
                        return await self._minted_wh(s, owners), "db_minted_fallback"
            total += self._balances[owner][0]
        return total, "held_balances"

    async def _market_time(self, s: AsyncSession, market: Market, now: datetime) -> tuple[int, str]:
        wall = int(now.timestamp())
        if self.settings.pricing_clock_mode != "follow_readings":
            return wall, "real"
        followed = await repo.follow_market_time(
            s, market, now, self.settings.clock_follow_window_seconds
        )
        return (followed, "follow_readings") if followed is not None else (wall, "real")

    async def _cell_input(self, mc: MarketCell, market_time: int, clock_mode: str) -> CellInput:
        lookup: Lookup
        if clock_mode == "follow_readings":
            lookup = await self.weather.lookup_history(mc.cell, market_time)
        else:
            lookup = self.weather.lookup(mc.cell.id, market_time)
        point = lookup.point
        return CellInput(
            cell_id=mc.cell.id,
            capacity_w=mc.capacity_w,
            meters=mc.meters,
            center_lon_e6=mc.cell.center_lon_e6,
            ghi_wm2=point.ghi_wm2 if point else Decimal(0),
            temp_c=point.temp_c if point else Decimal(25),
            cloud_cover_pct=point.cloud_cover_pct if point else None,
            weather_age_s=lookup.age_s,
            weather_source=lookup.source if point else "none",
        )

    # ------------------------------------------------------------------ weather
    async def poll_weather_once(self) -> str:
        async with self.sessionmaker() as s:
            await repo.ensure_default_market(s)
            cells = {
                mc.cell
                for m in await repo.list_markets(s, enabled_only=True)
                for mc in await repo.market_cells(s, m)
            }
        result = await self.weather.refresh(sorted(cells))
        return result.status

    # ------------------------------------------------------------------ one tick
    async def tick_market(self, market_id: str) -> Tick | None:
        async with self.sessionmaker() as s:
            market = await repo.get_market(s, market_id)
            if market is None or not market.enabled:
                return None
            rt = self._runtime(market.market_id)
            params = await self._params(s, market.market_id, rt)
            if not rt.restored:
                rows = await repo.recent_ticks(s, market.market_id, params.vol_window + 1)
                rt.state = restore_state(rows, params)
                rt.restored = True
                if rows:
                    log.info(
                        "pricing state restored", extra={"market": market.name, "ticks": len(rows)}
                    )

            now_dt = datetime.fromtimestamp(self._wall(), UTC)
            now_ts = int(now_dt.timestamp())
            market_time, clock_mode = await self._market_time(s, market, now_dt)
            mcells = await repo.market_cells(s, market)
            cells = tuple([await self._cell_input(mc, market_time, clock_mode) for mc in mcells])
            owners = {o for mc in mcells for o in mc.owners}
            inventory_wh, inventory_source = await self._inventory(
                s, market.market_id, owners, now_ts
            )
            bids = await repo.open_order_wh(s, market.market_id, OrderSide.BUY, now_ts)
            since = now_dt - timedelta(seconds=int(params.velocity_window_hours * 3600))
            fills = await repo.recent_fill_wh(s, market.market_id, since)
            inputs = MarketInputs(
                market_time=market_time,
                cells=cells,
                inventory_wh=inventory_wh,
                inventory_source=inventory_source,
                open_bid_wh=bids,
                recent_fill_wh=fills,
                onchain_band_bps=await self._onchain_band(rt),
            )

            cmd = await self.redis.getdel(publish.command_key(market.market_id))
            if cmd == "reset_breaker":
                rt.state = reset_breaker(rt.state, now_ts)
                log.warning("breaker reset by an administrator", extra={"market": market.name})

            rt.state, tick = step(rt.state, inputs, params, now_ts)
            rt.latest = tick
            await self._refresh_forecast(market, rt, mcells, inputs, params, clock_mode)

            quote: SignedQuote | None = None
            if self._pricer_key and self.chain is not None and not tick.halted:
                q = PriceQuote(market.market_id, tick.price, await self.clock.quote_timestamp())
                quote = sign_quote(
                    self._pricer_key, self.settings.chain_id, self.chain.exchange_address, q
                )
            rt.latest_quote = quote

            try:
                await repo.save_tick(s, market.market_id, tick, quote, clock_mode)
            except Exception:
                await s.rollback()
                log.exception("could not persist the tick; continuing")
            published = await publish.publish_tick(
                self.redis, tick, market.market_id, quote, clock_mode
            )
            await self._write_state(market, rt, tick, clock_mode, published.seq)
            self._observe(market, tick, clock_mode, published.seq)
            return tick

    async def tick_all(self) -> list[Tick]:
        async with self.sessionmaker() as s:
            await repo.ensure_default_market(s)
            ids = [m.market_id for m in await repo.list_markets(s, enabled_only=True)]
        out: list[Tick] = []
        for market_id in ids:
            try:
                tick = await self.tick_market(market_id)
            except Exception:
                log.exception("tick failed", extra={"market_id": market_id})
                continue
            if tick is not None:
                out.append(tick)
        return out

    async def _refresh_forecast(
        self,
        market: Market,
        rt: MarketRuntime,
        mcells: list[MarketCell],
        now_inputs: MarketInputs,
        params: PricingParams,
        clock_mode: str,
    ) -> None:
        """Hourly supply/demand/raw-price outlook for the next 24 h, cached in Redis for the API."""
        if self._wall() - rt.forecast_at < FORECAST_EVERY_S:
            return
        rt.forecast_at = self._wall()
        start = now_inputs.market_time - now_inputs.market_time % 3600
        hours: list[dict[str, Any]] = []
        for h in range(24):
            t = start + h * 3600
            cells = tuple([await self._cell_input(mc, t, clock_mode) for mc in mcells])
            if any(c.weather_source == "none" for c in cells if c.capacity_w > 0):
                continue  # no weather for that hour: leave a gap rather than guess
            hours.append(forecast_row(replace(now_inputs, market_time=t, cells=cells), params))
        body = {
            "marketId": market.market_id,
            "generatedAt": int(self._wall()),
            "marketTime": now_inputs.market_time,
            "note": "Raw price implied by the forecast: before smoothing and the per-tick limiter.",
            "hours": hours,
        }
        await self.redis.set(publish.forecast_key(market.market_id), json.dumps(body))

    def _observe(self, market: Market, tick: Tick, clock_mode: str, seq: int) -> None:
        name = market.name
        PRICE.labels(name).set(tick.price)
        PRICE_RAW.labels(name).set(tick.raw_price)
        BREAKER_STATE.labels(name).set(BREAKER_GAUGE[tick.breaker.mode])
        PRICING_TICKS.labels(name).inc()
        ages = [w["age_s"] for w in tick.drivers["weather"]]
        if ages:
            WEATHER_AGE.labels(name).set(max(ages))
        if tick.vol_bps is not None:
            PRICE_VOL.labels(name).set(float(tick.vol_bps))
        log.info(
            "tick",
            extra={
                "market": name,
                "seq": seq,
                "price": tick.price,
                "raw": tick.raw_price,
                "breaker": tick.breaker.mode.value,
                "supply_w": tick.supply_w,
                "demand_w": tick.demand_w,
                "clamps": tick.drivers["clamps"],
                "market_time": tick.market_time,
                "clock": clock_mode,
                "accelerated": tick.market_time < tick.ts - 60,
            },
        )
        for event in tick.events:
            PRICING_EVENTS.labels(event["kind"]).inc()
            level = logging.WARNING if event["kind"] != "breaker_recovered" else logging.INFO
            log.log(level, "pricing event", extra={"market": name, "event": event})

    async def _write_state(
        self, market: Market, rt: MarketRuntime, tick: Tick, clock_mode: str, seq: int
    ) -> None:
        o = rt.oracle
        body = {
            "market": market.name,
            "marketId": market.market_id,
            "ts": tick.ts,
            "seq": seq,
            "clockMode": clock_mode,
            "price": tick.price,
            "rawPrice": tick.raw_price,
            "breaker": tick.breaker.to_json(),
            "weatherStale": tick.weather_stale,
            "bandBps": tick.band_bps,
            "weather": [
                {"cell": w["cell"], "ageS": w["age_s"], "source": w["source"]}
                for w in tick.drivers["weather"]
            ],
            "weatherLastError": self.weather.last_error,
            "oracle": {
                "lastPrice": o.last_price,
                "lastPushAgoS": None if not o.last_wall else int(self._wall() - o.last_wall),
                "lastResult": o.last_result,
                "lastTx": o.last_tx,
                "failures": o.failures,
            },
        }
        await self.redis.set(publish.state_key(market.market_id), json.dumps(body))

    # ------------------------------------------------------------------ oracle
    async def push_oracle_once(self, *, force: bool = False) -> str:
        """Push the primary market's price to PriceOracle when it moved enough or the heartbeat
        is due. Never raises: a failed push backs off and the ticker is unaffected."""
        if self.pricer is None or self.chain is None:
            return "disabled"
        async with self.sessionmaker() as s:
            primary = next(
                (m for m in await repo.list_markets(s, enabled_only=True) if m.is_primary), None
            )
        if primary is None:
            return "no_primary"
        rt = self._runtime(primary.market_id)
        tick, o = rt.latest, rt.oracle
        if tick is None:
            return "no_tick"
        wall = self._wall()
        if wall < o.next_try:
            return "backoff"
        cfg = self.settings
        moved = (
            o.last_price is None
            or abs(tick.price - o.last_price) * 10_000
            >= cfg.oracle_push_threshold_bps * o.last_price
        )
        heartbeat = wall - o.last_wall >= cfg.oracle_push_heartbeat_seconds
        if not force and not (
            (moved or heartbeat) and wall - o.last_wall >= cfg.oracle_push_min_interval_seconds
        ):
            return "not_due"
        try:
            if o.last_onchain_ts == 0:
                latest = await self.chain.oracle_latest()
                o.last_onchain_ts = latest[1] if latest else 0
            block_ts = await self.chain.latest_block_timestamp()
            ts = block_ts  # never in the future; must strictly increase
            if ts <= o.last_onchain_ts:
                return "waiting_for_block"
            fn = self.chain.oracle.functions.updatePrice(tick.price, ts)
            signed = await self.pricer.submit(fn)
            rcpt = await self.pricer.wait([signed.tx_hash], 30.0, self.settings.mint_confirmations)
            if rcpt is None or int(rcpt["status"]) != 1:
                raise RuntimeError(f"oracle update {signed.tx_hash} not confirmed")
        except Exception as exc:
            o.failures += 1
            delay = min(60.0, 2.0**o.failures)
            o.next_try = wall + delay
            o.last_result = f"failed: {exc}"[:200]
            ORACLE_PUSHES.labels(result="failed").inc()
            log.warning("oracle push failed (%s); retry in %.0fs", exc, delay)
            return "failed"
        o.failures, o.next_try = 0, 0.0
        o.last_price, o.last_wall, o.last_onchain_ts = tick.price, wall, ts
        o.last_result, o.last_tx = "ok", signed.tx_hash
        ORACLE_PUSHES.labels(result="ok").inc()
        log.info(
            "oracle updated", extra={"price": tick.price, "timestamp": ts, "tx": signed.tx_hash}
        )
        return "pushed"

    # ------------------------------------------------------------------ loops
    async def _every(self, seconds: float, stop: asyncio.Event, fn: Callable[[], Any]) -> None:
        while not stop.is_set():
            started = time.monotonic()
            try:
                await fn()
            except Exception:
                log.exception("pricing loop iteration failed")
            wait = max(0.0, seconds - (time.monotonic() - started))
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), wait)

    async def run(self, stop: asyncio.Event) -> None:
        """Hold the leader lock and run the three loops; drop everything if the lock is lost."""
        lock = self.redis.lock(LEADER_LOCK, timeout=30)
        leader = False
        tasks: list[asyncio.Task[None]] = []
        cfg = self.settings
        try:
            while not stop.is_set():
                try:
                    if not leader:
                        leader = bool(await lock.acquire(blocking=False))
                        if leader:
                            log.info("pricing service became leader")
                            tasks = [
                                asyncio.create_task(
                                    self._every(
                                        cfg.weather_poll_seconds, stop, self.poll_weather_once
                                    ),
                                    name="weather",
                                ),
                                asyncio.create_task(
                                    self._every(cfg.pricing_tick_seconds, stop, self.tick_all),
                                    name="ticker",
                                ),
                                asyncio.create_task(
                                    self._every(5.0, stop, self.push_oracle_once), name="oracle"
                                ),
                            ]
                    else:
                        await lock.reacquire()
                except LockError:
                    leader = False
                    log.warning("pricing service lost the leader lock")
                    for t in tasks:
                        t.cancel()
                    tasks = []
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(stop.wait(), 5.0)
        finally:
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            if leader:
                with contextlib.suppress(LockError):
                    await lock.release()
