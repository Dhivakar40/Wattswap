"""Application settings, loaded from environment variables (pydantic-settings)."""

import json
from functools import lru_cache
from pathlib import Path
from typing import Literal

from eth_account import Account
from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

DEV_JWT_SECRET = "dev-only-insecure-jwt-secret-change-me-0123456789"  # noqa: S105


def _default_shared_dir() -> Path:
    """packages/shared in the repo checkout, or /srv/shared inside the API container."""
    for parent in Path(__file__).resolve().parents:
        if (parent / "packages" / "shared").is_dir():
            return parent / "packages" / "shared"
    return Path("/srv/shared")


CHAIN_KEYS = {31337: "anvil", 11155111: "sepolia"}


class ConfigError(RuntimeError):
    """Raised at startup when the configuration is unsafe or inconsistent."""


class Settings(BaseSettings):
    """Typed runtime configuration. Secrets come from the environment only."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Secure by default: an unset API_ENV is "production" everywhere it is read (strict secrets,
    # fail-fast chain checks, no missing-Origin /ws clients). Local dev sets API_ENV=development
    # explicitly (.env.example, docker-compose); the test suite sets "test".
    api_env: Literal["development", "test", "production"] = "production"
    api_log_level: str = "INFO"
    api_cors_origins: str = "http://localhost:3000"
    database_url: str = (
        "postgresql+asyncpg://wattswap:wattswap_dev_password@localhost:5432/wattswap"
    )
    redis_url: str = "redis://localhost:6379/0"
    rpc_url: str = "http://localhost:8545"
    chain_id: int = Field(default=31337, ge=1)

    # --- auth ---------------------------------------------------------------------------
    jwt_secret: SecretStr = SecretStr(DEV_JWT_SECRET)
    jwt_ttl_seconds: int = Field(default=12 * 3600, ge=60)
    session_cookie_name: str = "wattswap_session"
    # Cookies are always Secure. Browsers treat http://localhost as a secure context.
    session_cookie_secure: bool = True
    # Host-only (None) is right when the app and API share a registrable domain: SameSite=Lax
    # cookies flow between app.example.com and api.example.com because they are the same SITE.
    # Set e.g. ".example.com" only if another subdomain must also receive the cookie.
    session_cookie_domain: str | None = None
    siwe_domain: str = "localhost:3000"
    siwe_nonce_ttl_seconds: int = Field(default=300, ge=30)
    admin_addresses: str = ""

    # --- role keys (dev only; production would use a KMS/HSM) ----------------------------
    oracle_private_key: SecretStr | None = None  # EnergyToken MINTER_ROLE
    registrar_private_key: SecretStr | None = None  # MeterRegistry REGISTRAR_ROLE
    relayer_private_key: SecretStr | None = None  # Exchange RELAYER_ROLE (Phase 4)
    pricer_private_key: SecretStr | None = None  # PriceOracle PRICER_ROLE (Phase 3)

    # --- contracts ----------------------------------------------------------------------
    shared_dir: Path = Field(default_factory=_default_shared_dir)
    energy_token_address: str | None = None
    meter_registry_address: str | None = None
    exchange_address: str | None = None
    usdc_address: str | None = None
    price_oracle_address: str | None = None

    # --- meter pipeline -----------------------------------------------------------------
    reading_interval_seconds: int = Field(default=900, ge=1)
    mint_worker_enabled: bool = True
    mint_confirmations: int = Field(default=1, ge=1)
    mint_poll_seconds: float = Field(default=1.0, gt=0)
    mint_tx_timeout_seconds: float = Field(default=60.0, gt=0)
    mint_max_attempts: int = Field(default=5, ge=1)
    mint_backoff_base_seconds: float = Field(default=1.0, gt=0)
    mint_backoff_max_seconds: float = Field(default=60.0, gt=0)
    # Gas-price ceiling for every transaction this API sends (mint, settle, oracle). Fee bumps never
    # exceed it; above it, transactions are held and retried later, never sent.
    max_fee_per_gas_gwei: float = Field(default=100.0, gt=0)
    max_priority_fee_gwei: float = Field(default=5.0, gt=0)
    chain_params_ttl_seconds: float = Field(default=30.0, ge=0)

    # --- pricing engine -----------------------------------------------------------------
    pricing_enabled: bool = True
    pricing_embedded: bool = True  # False: run `python -m app.pricing.run` as its own process
    pricing_tick_seconds: int = Field(default=15, ge=1)
    weather_poll_seconds: int = Field(default=600, ge=300)  # stay well inside Open-Meteo limits
    open_meteo_forecast_url: str = "https://api.open-meteo.com/v1/forecast"
    open_meteo_archive_url: str = "https://archive-api.open-meteo.com/v1/archive"
    open_meteo_timeout_seconds: float = Field(default=10.0, gt=0)
    # real: market time is the wall clock. follow_readings: market time follows the newest
    # ingested simulated interval while readings keep arriving (accelerated demo mode).
    pricing_clock_mode: Literal["real", "follow_readings"] = "real"
    clock_follow_window_seconds: int = Field(default=120, ge=1)
    pricing_inventory_refresh_seconds: int = Field(default=30, ge=1)
    pricing_inventory_source: Literal["auto", "held", "open_sells"] = "auto"
    oracle_push_threshold_bps: int = Field(default=50, ge=1)
    oracle_push_heartbeat_seconds: int = Field(default=120, ge=10)
    oracle_push_min_interval_seconds: int = Field(default=30, ge=1)

    # --- trading (order book, matcher, relayer, listener) --------------------------------
    trading_enabled: bool = True
    trading_embedded: bool = True  # False: run `python -m app.trading.run` as its own process
    min_order_wh: int = Field(default=100, ge=1)  # 0.1 kWh
    min_fill_wh: int = Field(default=100, ge=1)
    min_fill_cost: int = Field(default=1000, ge=1)  # mUSDC base units; keeps gas worth spending
    min_limit_price: int = Field(default=1000, ge=1)  # mUSDC base units per kWh
    max_limit_price: int = Field(default=100_000_000, ge=1)  # 100 mUSDC per kWh
    min_order_life_seconds: int = Field(default=60, ge=1)
    max_order_life_seconds: int = Field(default=7 * 86400, ge=60)
    expiry_margin_seconds: int = Field(default=90, ge=0)  # must outlive submit + mining
    max_open_orders_per_maker: int = Field(default=50, ge=1)
    rate_limit_orders: int = Field(default=30, ge=1)  # order placements per address per window
    match_poll_ms: int = Field(default=500, ge=50)
    quote_safety_margin_seconds: int = Field(default=60, ge=1)
    band_safety_pct: int = Field(default=90, ge=1, le=100)  # share of the on-chain band we use
    settle_max_in_flight: int = Field(default=4, ge=1)
    settle_hold_retry_seconds: float = Field(default=5.0, gt=0)  # gas above the ceiling: retry
    settle_max_attempts: int = Field(default=5, ge=1)
    settle_bump_after_seconds: float = Field(default=20.0, gt=0)
    settle_backoff_base_seconds: float = Field(default=1.0, gt=0)
    settle_backoff_max_seconds: float = Field(default=60.0, gt=0)
    relayer_poll_seconds: float = Field(default=0.5, gt=0)
    pair_cooldown_seconds: int = Field(default=30, ge=1)
    pair_cooldown_max_seconds: int = Field(default=600, ge=1)
    funding_sweep_seconds: float = Field(default=20.0, gt=0)
    listener_confirmations: int = Field(default=1, ge=1)  # use 6 on Sepolia
    listener_start_block: int = Field(default=0, ge=0)  # set to the deploy block on Sepolia
    listener_reorg_window: int = Field(default=64, ge=2)
    listener_batch_blocks: int = Field(default=500, ge=1)
    listener_poll_seconds: float = Field(default=1.0, gt=0)
    reconcile_seconds: float = Field(default=120.0, gt=0)
    trading_leader_ttl_seconds: int = Field(default=30, ge=5)

    # --- rate limits (requests per window) ----------------------------------------------
    rate_limit_window_seconds: int = Field(default=60, ge=1)
    rate_limit_ip: int = Field(default=300, ge=1)
    rate_limit_auth: int = Field(default=30, ge=1)
    rate_limit_user: int = Field(default=120, ge=1)
    rate_limit_meter_ingest: int = Field(default=60, ge=1)

    # --- WebSocket gateway (Phase 5) ------------------------------------------------------
    # Origins allowed to open /ws besides API_CORS_ORIGINS (comma-separated).
    ws_extra_origins: str = ""
    ws_max_subscriptions: int = Field(default=20, ge=1)
    ws_max_message_bytes: int = Field(default=4096, ge=64)
    ws_rate_per_window: int = Field(default=20, ge=1)  # sustained inbound messages per window
    ws_rate_window_seconds: float = Field(default=10.0, gt=0)
    ws_rate_burst: int = Field(default=40, ge=1)
    ws_rate_strikes: int = Field(default=3, ge=1)  # rate-limit errors before 4429
    ws_max_conn_per_ip: int = Field(default=20, ge=1)
    ws_max_conn_per_address: int = Field(default=10, ge=1)
    ws_send_queue: int = Field(default=256, ge=2)
    ws_send_timeout_seconds: float = Field(default=10.0, gt=0)
    ws_ping_seconds: float = Field(default=20.0, gt=0)  # server ping; closed after 2 missed
    ws_denylist_poll_seconds: float = Field(default=30.0, gt=0)
    ws_resume_max_messages: int = Field(default=1000, ge=1)
    # Dev-only synthetic `loadtest:{name}` channels for `make ws-load`; refused in production.
    ws_loadtest_enabled: bool = False
    # Retained stream windows (resume): count and age per channel class.
    ws_retain_public_len: int = Field(default=5000, ge=10)
    ws_retain_public_age_seconds: int = Field(default=600, ge=10)
    ws_retain_user_len: int = Field(default=1000, ge=10)
    ws_retain_user_age_seconds: int = Field(default=1800, ge=10)
    ws_retain_price_len: int = Field(default=500, ge=10)
    ws_retain_price_age_seconds: int = Field(default=3600, ge=10)

    @field_validator("session_cookie_domain")
    @classmethod
    def _empty_cookie_domain_is_host_only(cls, value: str | None) -> str | None:
        return value or None  # `SESSION_COOKIE_DOMAIN=` in .env means "not set"

    @property
    def ws_origins(self) -> frozenset[str]:
        extra = [o.strip() for o in self.ws_extra_origins.split(",") if o.strip()]
        return frozenset(o.rstrip("/").lower() for o in [*self.cors_origins, *extra])

    @property
    def ws_allow_missing_origin(self) -> bool:
        """A /ws handshake without an Origin header (a non-browser client) is accepted only in
        development. An unset API_ENV is production, so it is refused."""
        return self.api_env == "development"

    @property
    def cors_origins(self) -> list[str]:
        return [o.strip() for o in self.api_cors_origins.split(",") if o.strip()]

    @property
    def admin_address_set(self) -> frozenset[str]:
        return frozenset(a.strip().lower() for a in self.admin_addresses.split(",") if a.strip())

    @property
    def chain_key(self) -> str:
        return CHAIN_KEYS.get(self.chain_id, "anvil")

    def role_key_addresses(self) -> dict[str, str]:
        """Address of every configured role key, by role name."""
        keys = {
            "oracle": self.oracle_private_key,
            "registrar": self.registrar_private_key,
            "relayer": self.relayer_private_key,
            "pricer": self.pricer_private_key,
        }
        out: dict[str, str] = {}
        for role, secret in keys.items():
            if secret is not None and secret.get_secret_value():
                try:
                    out[role] = Account.from_key(secret.get_secret_value()).address
                except ValueError as exc:
                    raise ConfigError(
                        f"{role.upper()}_PRIVATE_KEY is not a valid private key"
                    ) from exc
        return out

    @model_validator(mode="after")
    def _check_safety(self) -> "Settings":
        if self.api_env == "production" and self.ws_loadtest_enabled:
            raise ConfigError("WS_LOADTEST_ENABLED must not be set in production")
        if self.api_env == "production":
            secret = self.jwt_secret.get_secret_value()
            if secret == DEV_JWT_SECRET or len(secret) < 32:
                raise ConfigError(
                    "JWT_SECRET must be set to a random value >= 32 chars in production"
                )
        assert_distinct_role_keys(self)
        if self.pricing_enabled:
            # Same rule the admin API enforces: at this tick rate the per-tick limiter must not be
            # able to reach the breaker threshold inside its window. Misconfiguration = no start.
            from app.pricing.params import PricingParams, cross_check

            problems = cross_check(PricingParams(tick_seconds=self.pricing_tick_seconds))
            if problems:
                raise ConfigError(
                    f"PRICING_TICK_SECONDS={self.pricing_tick_seconds} is inconsistent with the "
                    f"default pricing parameters: {'; '.join(problems.values())}"
                )
        return self

    def load_abi(self, name: str) -> list[dict[str, object]]:
        path = self.shared_dir / "src" / "abis" / "json" / f"{name}.json"
        abi: list[dict[str, object]] = json.loads(path.read_text(encoding="utf-8"))
        return abi

    def contract_address(self, name: str) -> str:
        """Deployed address: explicit env override first, then packages/shared addresses."""
        override = {
            "EnergyToken": self.energy_token_address,
            "MeterRegistry": self.meter_registry_address,
            "WattSwapExchange": self.exchange_address,
            "MockUSDC": self.usdc_address,
            "PriceOracle": self.price_oracle_address,
        }.get(name)
        if override:
            return override
        book = json.loads((self.shared_dir / "src" / "addresses.json").read_text(encoding="utf-8"))
        addr: str | None = book.get(self.chain_key, {}).get(name)
        if not addr:
            raise ConfigError(
                f"no {name} address for chain {self.chain_id}; run `make deploy-local`"
            )
        return addr


def assert_distinct_role_keys(settings: Settings) -> None:
    """Refuse to boot if two role keys resolve to the same address (key separation)."""
    seen: dict[str, str] = {}
    for role, address in settings.role_key_addresses().items():
        other = seen.get(address)
        if other is not None:
            raise ConfigError(
                f"role keys must be distinct: {other} and {role} resolve to the same address "
                f"{address}"
            )
        seen[address] = role


@lru_cache
def get_settings() -> Settings:
    return Settings()
