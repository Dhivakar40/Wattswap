import pytest

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
