from pathlib import Path

import pytest
from pydantic import SecretStr

from app.core.config import Settings


def test_cors_origins_split(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("API_CORS_ORIGINS", "http://a.test, http://b.test ,")
    assert Settings().cors_origins == ["http://a.test", "http://b.test"]


def test_defaults() -> None:
    assert Settings().chain_id >= 1


def test_empty_session_cookie_domain_means_host_only() -> None:
    assert Settings(_env_file=None, session_cookie_domain="").session_cookie_domain is None
    assert Settings(_env_file=None, session_cookie_domain=".example.com").session_cookie_domain == (
        ".example.com"
    )


def test_unset_api_env_behaves_as_production_everywhere_it_is_read(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Every place that reads `api_env`: config validation, /ws Origin policy, the loadtest
    channel, chain construction in the API, in the pricing bundle and in the pricing runner."""
    import asyncio

    from app.core.config import ConfigError
    from app.main import _build_chain
    from app.pricing import run as pricing_run
    from app.pricing.factory import build_pricing

    monkeypatch.delenv("API_ENV", raising=False)
    monkeypatch.delenv("JWT_SECRET", raising=False)
    good = SecretStr("x" * 40)

    # config.py: the default is production, and production insists on a real JWT secret
    with pytest.raises(ConfigError, match="JWT_SECRET"):
        Settings(_env_file=None)
    s = Settings(_env_file=None, jwt_secret=good, shared_dir=tmp_path)  # no ABIs here
    assert s.api_env == "production"
    # config.py: the dev-only loadtest channel is refused
    with pytest.raises(ConfigError, match="WS_LOADTEST_ENABLED"):
        Settings(_env_file=None, jwt_secret=good, ws_loadtest_enabled=True)
    # /ws handshake policy: a missing Origin is refused
    assert not s.ws_allow_missing_origin
    # main.py: a chain that cannot be built is fatal (development would log and continue)
    with pytest.raises((ConfigError, FileNotFoundError)):
        asyncio.run(_build_chain(s, None))  # type: ignore[arg-type]
    # pricing/factory.py: production requires the pricer key and a chain
    with pytest.raises(ConfigError, match="PRICER_PRIVATE_KEY"):
        build_pricing(s, None, None, None)  # type: ignore[arg-type]
    # pricing/run.py: the standalone runner re-raises instead of running without a chain
    monkeypatch.setattr(pricing_run, "get_settings", lambda: s)
    with pytest.raises((ConfigError, FileNotFoundError)):
        asyncio.run(pricing_run.main())

    # ... while an explicit development setting keeps the lenient behaviour
    dev = Settings(_env_file=None, api_env="development", shared_dir=tmp_path)
    assert dev.ws_allow_missing_origin
    assert asyncio.run(_build_chain(dev, None)) is None  # type: ignore[arg-type]
