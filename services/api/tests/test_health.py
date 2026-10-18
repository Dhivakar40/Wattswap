from collections.abc import AsyncIterator

import pytest
from httpx import ASGITransport, AsyncClient

from app.main import create_app


@pytest.fixture
async def client() -> AsyncIterator[AsyncClient]:
    async with AsyncClient(transport=ASGITransport(app=create_app()), base_url="http://t") as c:
        yield c


async def test_health_ok(client: AsyncClient) -> None:
    r = await client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


async def test_request_id_is_echoed(client: AsyncClient) -> None:
    r = await client.get("/health", headers={"x-request-id": "abc123"})
    assert r.headers["x-request-id"] == "abc123"


async def test_request_id_generated(client: AsyncClient) -> None:
    r = await client.get("/health")
    assert len(r.headers["x-request-id"]) == 32


async def test_metrics_exposed(client: AsyncClient) -> None:
    r = await client.get("/metrics")
    assert r.status_code == 200
    assert "python_info" in r.text
