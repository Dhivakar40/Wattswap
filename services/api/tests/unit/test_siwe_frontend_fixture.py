"""Cross-language SIWE gate, backend half.

`packages/shared/vectors/siwe-frontend.json` is written by the web app's hand-built EIP-4361 builder
(apps/web/tests/siwe-fixture.test.ts). Every message in it must be accepted by the same Python `siwe`
library production login uses: parsed field by field, re-serialised to the identical bytes, and its
signature verified for the expected signer. Any mismatch fails CI.
"""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from siwe import SiweMessage
from siwe.siwe import ExpiredMessage, InvalidSignature, NotYetValidMessage

FIXTURE = Path(__file__).parents[4] / "packages" / "shared" / "vectors" / "siwe-frontend.json"
CASES: list[dict[str, Any]] = json.loads(FIXTURE.read_text(encoding="utf-8"))["cases"]


def _dt(value: str | None) -> datetime | None:
    return None if value is None else datetime.fromisoformat(value.replace("Z", "+00:00"))


def _iso(value: Any) -> str | None:
    """siwe wraps timestamps; compare as UTC instants."""
    if value is None:
        return None
    raw = getattr(value, "_datetime", value)
    return str(raw.astimezone(UTC).isoformat())


def test_fixture_is_not_empty() -> None:
    assert len(CASES) >= 7


@pytest.mark.parametrize("case", CASES, ids=[c["name"] for c in CASES])
def test_python_siwe_parses_and_verifies_the_frontend_message(case: dict[str, Any]) -> None:
    message: str = case["message"]
    want = case["expect"]
    parsed = SiweMessage.from_message(message)

    assert parsed.scheme == want["scheme"]
    assert parsed.domain == want["domain"]
    assert parsed.address == want["address"]  # EIP-55 checksummed, exactly as signed
    assert parsed.statement == want["statement"]
    assert parsed.uri == want["uri"]
    assert parsed.version == "1"
    assert parsed.chain_id == want["chain_id"]
    assert parsed.nonce == want["nonce"]
    assert _iso(parsed.issued_at) == _iso(_dt(want["issued_at"]))
    assert _iso(parsed.expiration_time) == _iso(_dt(want["expiration_time"]))
    assert _iso(parsed.not_before) == _iso(_dt(want["not_before"]))
    assert parsed.request_id == want["request_id"]
    assert list(parsed.resources or []) == want["resources"]

    # The strongest check: the library's own serialiser reproduces the frontend's bytes.
    assert parsed.prepare_message() == message

    issued = _dt(want["issued_at"])
    assert issued is not None
    parsed.verify(
        case["signature"],
        scheme=want["scheme"],
        domain=want["domain"],
        nonce=want["nonce"],
        timestamp=issued + timedelta(seconds=30),
    )


@pytest.mark.parametrize("case", CASES, ids=[c["name"] for c in CASES])
def test_a_tampered_message_or_wrong_signer_is_rejected(case: dict[str, Any]) -> None:
    issued = _dt(case["expect"]["issued_at"])
    assert issued is not None
    at = issued + timedelta(seconds=30)
    tampered = SiweMessage.from_message(case["message"].replace("Nonce: ", "Nonce: x", 1))
    with pytest.raises(InvalidSignature):
        tampered.verify(case["signature"], timestamp=at)
    # a different case's signature never validates this message
    other = next(c for c in CASES if c["signer"] != case["signer"])
    with pytest.raises(InvalidSignature):
        SiweMessage.from_message(case["message"]).verify(other["signature"], timestamp=at)


def test_expiry_and_not_before_are_enforced() -> None:
    with_expiry = next(c for c in CASES if c["expect"]["expiration_time"] is not None)
    expires = _dt(with_expiry["expect"]["expiration_time"])
    assert expires is not None
    with pytest.raises(ExpiredMessage):
        SiweMessage.from_message(with_expiry["message"]).verify(
            with_expiry["signature"], timestamp=expires + timedelta(seconds=1)
        )
    with_nbf = next(c for c in CASES if c["expect"]["not_before"] is not None)
    nbf = _dt(with_nbf["expect"]["not_before"])
    assert nbf is not None
    with pytest.raises(NotYetValidMessage):
        SiweMessage.from_message(with_nbf["message"]).verify(
            with_nbf["signature"], timestamp=nbf - timedelta(seconds=1)
        )


def test_the_app_login_shape_is_what_the_backend_requires() -> None:
    """The first case is the exact message the app sends: it carries everything verify_login demands
    (chain id, issued-at, expiration-time) and the API's configured domain format."""
    login = CASES[0]
    parsed = SiweMessage.from_message(login["message"])
    assert parsed.expiration_time is not None and parsed.issued_at is not None
    assert parsed.domain == "localhost:3000"
    assert parsed.statement == "Sign in to WattSwap (testnet demo)."
