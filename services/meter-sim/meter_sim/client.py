"""HTTP client for the API's ingest endpoint (the simulator's only way into the system)."""

import asyncio
import logging
from dataclasses import dataclass
from typing import Any

import httpx

from meter_sim.signing import SignedReading

log = logging.getLogger("meter_sim.client")


class IngestRejectedError(Exception):
    """The API refused the reading permanently (4xx other than rate limiting)."""

    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(f"{status} {code}: {message}")
        self.status = status
        self.code = code


@dataclass(frozen=True)
class IngestResult:
    duplicate: bool
    mint_status: str | None


class IngestClient:
    def __init__(
        self,
        client: httpx.AsyncClient,
        *,
        retries: int = 4,
        backoff_s: float = 0.5,
    ) -> None:
        self._client = client
        self._retries = retries
        self._backoff = backoff_s

    async def config(self) -> dict[str, Any]:
        resp = await self._client.get("/api/v1/config")
        resp.raise_for_status()
        body: dict[str, Any] = resp.json()
        return body

    async def post(self, reading: SignedReading) -> IngestResult:
        """POST a reading. Safe to retry: the API is idempotent on reading_id."""
        for attempt in range(self._retries + 1):
            try:
                resp = await self._client.post("/api/v1/readings", json=reading.to_json())
            except httpx.TransportError as exc:
                if attempt == self._retries:
                    raise
                log.warning("ingest transport error (%s); retrying", exc)
                await asyncio.sleep(self._backoff * 2**attempt)
                continue
            if resp.status_code in (200, 202):
                body = resp.json()
                mint = body["reading"].get("mint")
                return IngestResult(bool(body["duplicate"]), mint["status"] if mint else None)
            retryable = resp.status_code == 429 or resp.status_code >= 500
            if retryable and attempt < self._retries:
                retry_after = float(resp.headers.get("retry-after", self._backoff * 2**attempt))
                await asyncio.sleep(retry_after)
                continue
            err = resp.json().get("error", {}) if resp.content else {}
            raise IngestRejectedError(
                resp.status_code, str(err.get("code", "unknown")), str(err.get("message", ""))
            )
        raise AssertionError("unreachable")  # pragma: no cover
