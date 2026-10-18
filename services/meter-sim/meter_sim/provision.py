"""DEV-ONLY helpers: sign in as an owner wallet (SIWE) and register a simulated meter.

In the real product the owner does this from the web UI with their own wallet. This module
exists so the demo and the end-to-end tests can do the same through the public API. It needs
the OWNER's private key, which a real meter would never have: do not use it outside dev/test.
"""

import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
from eth_account import Account
from eth_account.messages import encode_defunct
from eth_utils import to_checksum_address

COOKIE_NAME = "wattswap_session"


def format_time(t: datetime) -> str:
    return t.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.") + f"{t.microsecond // 1000:03d}Z"


def build_siwe_message(
    *,
    domain: str,
    address: str,
    uri: str,
    chain_id: int,
    nonce: str,
    issued_at: datetime,
    expires_in: timedelta = timedelta(minutes=5),
    statement: str = "Sign in to WattSwap (testnet demo).",
) -> str:
    """EIP-4361 message text."""
    return (
        f"{domain} wants you to sign in with your Ethereum account:\n"
        f"{to_checksum_address(address)}\n\n"
        f"{statement}\n\n"
        f"URI: {uri}\n"
        f"Version: 1\n"
        f"Chain ID: {chain_id}\n"
        f"Nonce: {nonce}\n"
        f"Issued At: {format_time(issued_at)}\n"
        f"Expiration Time: {format_time(issued_at + expires_in)}"
    )


@dataclass
class Session:
    """An authenticated API session. The cookie is attached explicitly because it is `Secure`
    and the dev API is served over plain http://localhost."""

    client: httpx.AsyncClient
    cookie: str
    address: str

    @property
    def headers(self) -> dict[str, str]:
        return {"cookie": f"{COOKIE_NAME}={self.cookie}"}


async def siwe_login(
    client: httpx.AsyncClient, owner_key: str, *, domain: str, chain_id: int
) -> Session:
    account = Account.from_key(owner_key)
    nonce_resp = await client.get("/api/v1/auth/nonce")
    nonce_resp.raise_for_status()
    message = build_siwe_message(
        domain=domain,
        address=account.address,
        uri=f"http://{domain}",
        chain_id=chain_id,
        nonce=nonce_resp.json()["nonce"],
        issued_at=datetime.now(UTC),
    )
    signature = account.sign_message(encode_defunct(text=message)).signature.hex()
    resp = await client.post(
        "/api/v1/auth/login", json={"message": message, "signature": "0x" + signature}
    )
    resp.raise_for_status()
    cookie = resp.cookies.get(COOKIE_NAME)
    if cookie is None:  # Secure cookie over http: the jar may not store it; read the header
        raw = resp.headers.get("set-cookie", "")
        cookie = raw.split(f"{COOKIE_NAME}=", 1)[1].split(";", 1)[0] if COOKIE_NAME in raw else None
    if cookie is None:
        raise RuntimeError("login succeeded but no session cookie was returned")
    return Session(client=client, cookie=cookie, address=account.address)


async def register_meter(
    session: Session, *, label: str, device_address: str, capacity_w: int, lat: float, lon: float
) -> dict[str, Any]:
    resp = await session.client.post(
        "/api/v1/meters",
        headers=session.headers,
        json={
            "label": label,
            "device_signer": device_address,
            "rated_capacity_w": capacity_w,
            "lat": lat,
            "lon": lon,
        },
    )
    resp.raise_for_status()
    body: dict[str, Any] = resp.json()
    return body


def save_meters_config(path: Path, entry: dict[str, Any]) -> None:
    """Append a meter entry to the simulator's meters.json."""
    data: dict[str, Any] = {"meters": []}
    if path.exists():
        data = json.loads(path.read_text(encoding="utf-8"))
    data["meters"] = [m for m in data["meters"] if m["meter_id"] != entry["meter_id"]] + [entry]
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
