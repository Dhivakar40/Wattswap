"""web3.py v7 client: contracts, cached on-chain parameters and startup verification."""

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Any

from eth_account import Account
from eth_account.signers.local import LocalAccount
from eth_utils import to_checksum_address
from web3 import AsyncHTTPProvider, AsyncWeb3
from web3.contract import AsyncContract
from web3.exceptions import ContractLogicError

from app.chain.revert import RevertDecoder, RevertInfo
from app.core.config import Settings

log = logging.getLogger("wattswap.chain")


@dataclass(frozen=True)
class ExchangeParams:
    """Admin-settable exchange/oracle parameters the trading engine must respect."""

    band_bps: int
    fee_bps: int
    max_staleness: int
    treasury: str
    paused: bool


@dataclass(frozen=True)
class MakerSnapshot:
    """One maker's funds and order state read at ONE block, so the numbers are consistent."""

    block: int
    watt_balance: int
    watt_allowance: int
    usdc_balance: int
    usdc_allowance: int
    min_nonce: int
    filled: dict[str, int]
    cancelled: dict[str, bool]


@dataclass(frozen=True)
class PlausibilityParams:
    safety_bps: int
    min_interval_seconds: int
    max_interval_seconds: int

    def max_energy_wh(self, rated_capacity_w: int, interval_seconds: int) -> int:
        """Mirror of MeterRegistry.maxEnergyWh (integer floor division, same order of ops)."""
        return rated_capacity_w * interval_seconds * self.safety_bps // (3600 * 10_000)


class ChainClient:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.w3 = AsyncWeb3(AsyncHTTPProvider(settings.rpc_url, request_kwargs={"timeout": 30}))
        self.token_address = to_checksum_address(settings.contract_address("EnergyToken"))
        self.registry_address = to_checksum_address(settings.contract_address("MeterRegistry"))
        token_abi = settings.load_abi("EnergyToken")
        registry_abi = settings.load_abi("MeterRegistry")
        self.token: AsyncContract = self.w3.eth.contract(address=self.token_address, abi=token_abi)
        self.registry: AsyncContract = self.w3.eth.contract(
            address=self.registry_address, abi=registry_abi
        )
        exchange_abi = settings.load_abi("WattSwapExchange")
        oracle_abi = settings.load_abi("PriceOracle")
        self.exchange_address = to_checksum_address(settings.contract_address("WattSwapExchange"))
        self.oracle_address = to_checksum_address(settings.contract_address("PriceOracle"))
        self.exchange: AsyncContract = self.w3.eth.contract(
            address=self.exchange_address, abi=exchange_abi
        )
        self.oracle: AsyncContract = self.w3.eth.contract(
            address=self.oracle_address, abi=oracle_abi
        )
        usdc_abi = settings.load_abi("MockUSDC")
        self.usdc_address = to_checksum_address(settings.contract_address("MockUSDC"))
        self.usdc: AsyncContract = self.w3.eth.contract(address=self.usdc_address, abi=usdc_abi)
        self.exchange_abi = exchange_abi
        self.decoder = RevertDecoder([token_abi, registry_abi, exchange_abi, oracle_abi, usdc_abi])
        self._params: PlausibilityParams | None = None
        self._params_at = 0.0
        self._xparams: ExchangeParams | None = None
        self._xparams_at = 0.0

    # ------------------------------------------------------------------ accounts
    @staticmethod
    def account(secret: str) -> LocalAccount:
        account: LocalAccount = Account.from_key(secret)
        return account

    # ------------------------------------------------------------------ reads
    async def plausibility(self) -> PlausibilityParams:
        """Registry plausibility parameters, cached briefly (they are admin-settable)."""
        now = time.monotonic()
        ttl = self.settings.chain_params_ttl_seconds
        if self._params is None or now - self._params_at > ttl:
            safety, lo, hi = (
                await self.registry.functions.safetyBps().call(),
                await self.registry.functions.minIntervalSeconds().call(),
                await self.registry.functions.maxIntervalSeconds().call(),
            )
            self._params = PlausibilityParams(int(safety), int(lo), int(hi))
            self._params_at = now
        return self._params

    async def reading_minted(self, reading_id: str) -> bool:
        return bool(await self.token.functions.readingMinted(reading_id).call())

    async def last_interval_end(self, meter_id: str) -> int:
        return int(await self.registry.functions.lastIntervalEnd(meter_id).call())

    async def onchain_band_bps(self) -> int:
        return int(await self.exchange.functions.bandBps().call())

    async def oracle_latest(self) -> tuple[int, int] | None:
        """(price, timestamp) of the oracle, or None if it has never been updated."""
        try:
            price, ts = await self.oracle.functions.latest().call()
        except ContractLogicError:  # NoPrice()
            return None
        return int(price), int(ts)

    async def exchange_params(self, *, fresh: bool = False) -> ExchangeParams:
        """Band, fee, quote staleness window and pause flag, cached briefly (all admin-settable)."""
        now = time.monotonic()
        ttl = self.settings.chain_params_ttl_seconds
        if fresh or self._xparams is None or now - self._xparams_at > ttl:
            band, fee, stale, treasury, paused = await asyncio.gather(
                self.exchange.functions.bandBps().call(),
                self.exchange.functions.feeBps().call(),
                self.oracle.functions.maxStaleness().call(),
                self.exchange.functions.treasury().call(),
                self.exchange.functions.paused().call(),
            )
            self._xparams = ExchangeParams(
                int(band), int(fee), int(stale), str(treasury).lower(), bool(paused)
            )
            self._xparams_at = now
        return self._xparams

    def invalidate_params(self) -> None:
        self._xparams = None
        self._params = None

    async def market_enabled(self, market_id: str) -> bool:
        return bool(await self.exchange.functions.marketEnabled(market_id).call())

    async def maker_snapshot(
        self, maker: str, order_hashes: list[str], block: int | None = None
    ) -> MakerSnapshot:
        """Balances, allowances, cancel state and fills for `maker`, all at one block number."""
        if block is None:
            block = int(await self.w3.eth.block_number)
        who = to_checksum_address(maker)
        ex = self.exchange_address
        at: Any = block
        head = await asyncio.gather(
            self.token.functions.balanceOf(who).call(block_identifier=at),
            self.token.functions.allowance(who, ex).call(block_identifier=at),
            self.usdc.functions.balanceOf(who).call(block_identifier=at),
            self.usdc.functions.allowance(who, ex).call(block_identifier=at),
            self.exchange.functions.minNonce(who).call(block_identifier=at),
        )
        filled, cancelled = await asyncio.gather(
            asyncio.gather(
                *(self.exchange.functions.filled(h).call(block_identifier=at) for h in order_hashes)
            ),
            asyncio.gather(
                *(
                    self.exchange.functions.cancelled(h).call(block_identifier=at)
                    for h in order_hashes
                )
            ),
        )
        return MakerSnapshot(
            block=block,
            watt_balance=int(head[0]),
            watt_allowance=int(head[1]),
            usdc_balance=int(head[2]),
            usdc_allowance=int(head[3]),
            min_nonce=int(head[4]),
            filled={h: int(f) for h, f in zip(order_hashes, filled, strict=True)},
            cancelled={h: bool(c) for h, c in zip(order_hashes, cancelled, strict=True)},
        )

    async def balance_of(self, address: str) -> int:
        return int(await self.token.functions.balanceOf(to_checksum_address(address)).call())

    async def latest_block_timestamp(self) -> int:
        block = await self.w3.eth.get_block("latest")
        return int(block["timestamp"])

    # ------------------------------------------------------------------ errors
    def revert_from_exception(self, exc: Exception) -> RevertInfo | None:
        if isinstance(exc, ContractLogicError):
            data: Any = getattr(exc, "data", None)
            if isinstance(data, str):
                return self.decoder.decode(data)
        return None

    # ------------------------------------------------------------------ startup checks
    async def verify(self, roles: dict[str, str]) -> list[str]:
        """Returns a list of problems; empty means the chain is wired up correctly."""
        problems: list[str] = []
        chain_id = await self.w3.eth.chain_id
        if chain_id != self.settings.chain_id:
            problems.append(f"rpc chain id {chain_id} != CHAIN_ID {self.settings.chain_id}")
        for addr, name in (
            (self.token_address, "EnergyToken"),
            (self.registry_address, "MeterRegistry"),
        ):
            if not await self.w3.eth.get_code(addr):
                problems.append(f"no contract code at {name} {addr}")
        if problems:
            return problems
        minter_role = await self.token.functions.MINTER_ROLE().call()
        registrar_role = await self.registry.functions.REGISTRAR_ROLE().call()
        recorder_role = await self.registry.functions.RECORDER_ROLE().call()
        if (
            "oracle" in roles
            and not await self.token.functions.hasRole(minter_role, roles["oracle"]).call()
        ):
            problems.append(f"oracle {roles['oracle']} lacks MINTER_ROLE on EnergyToken")
        if (
            "registrar" in roles
            and not await self.registry.functions.hasRole(registrar_role, roles["registrar"]).call()
        ):
            problems.append(f"registrar {roles['registrar']} lacks REGISTRAR_ROLE on MeterRegistry")
        if not await self.registry.functions.hasRole(recorder_role, self.token_address).call():
            problems.append("EnergyToken lacks RECORDER_ROLE on MeterRegistry")
        if "pricer" in roles:
            pricer_role = await self.oracle.functions.PRICER_ROLE().call()
            if not await self.oracle.functions.hasRole(pricer_role, roles["pricer"]).call():
                problems.append(f"pricer {roles['pricer']} lacks PRICER_ROLE on PriceOracle")
        return problems

    async def close(self) -> None:
        provider: Any = self.w3.provider
        disconnect = getattr(provider, "disconnect", None)
        if disconnect is not None:
            await disconnect()
