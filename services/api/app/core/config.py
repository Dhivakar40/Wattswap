"""Application settings, loaded from environment variables (pydantic-settings)."""

from functools import lru_cache
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Typed runtime configuration. Secrets come from the environment only."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    api_env: Literal["development", "test", "production"] = "development"
    api_log_level: str = "INFO"
    api_cors_origins: str = "http://localhost:3000"
    database_url: str = (
        "postgresql+asyncpg://wattswap:wattswap_dev_password@localhost:5432/wattswap"
    )
    redis_url: str = "redis://localhost:6379/0"
    rpc_url: str = "http://localhost:8545"
    chain_id: int = Field(default=31337, ge=1)

    @property
    def cors_origins(self) -> list[str]:
        return [o.strip() for o in self.api_cors_origins.split(",") if o.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
