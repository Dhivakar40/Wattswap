"""Sign-In with Ethereum (EIP-4361) verification, including EIP-1271 contract wallets."""

import asyncio
import secrets
from datetime import UTC, datetime, timedelta

import redis.asyncio as aioredis
from siwe import SiweMessage
from siwe.siwe import VerificationError
from web3 import HTTPProvider

from app.core.config import Settings
from app.core.errors import AppError

MAX_ISSUED_AT_AGE = timedelta(minutes=10)
MAX_CLOCK_SKEW = timedelta(seconds=60)


def _nonce_key(nonce: str) -> str:
    return f"siwe:nonce:{nonce}"


async def create_nonce(redis: aioredis.Redis, settings: Settings) -> str:
    nonce = secrets.token_hex(16)  # 32 alphanumeric chars; EIP-4361 requires >= 8
    await redis.set(_nonce_key(nonce), "1", ex=settings.siwe_nonce_ttl_seconds)
    return nonce


def _invalid(message: str) -> AppError:
    return AppError(401, "invalid_siwe", message)


async def verify_login(
    redis: aioredis.Redis, settings: Settings, message: str, signature: str
) -> str:
    """Validate a SIWE message + signature and return the signer's lowercase address.

    The nonce is consumed (GETDEL) before verification, so every nonce works exactly once,
    even if the signature turns out to be bad.
    """
    try:
        parsed = SiweMessage.from_message(message)
    except Exception as exc:  # the parser raises several unrelated exception types
        raise _invalid("malformed SIWE message") from exc

    if parsed.chain_id != settings.chain_id:
        raise _invalid(f"wrong chain id {parsed.chain_id}, expected {settings.chain_id}")
    if parsed.domain != settings.siwe_domain:
        raise _invalid("domain mismatch")

    now = datetime.now(UTC)
    issued = parsed.issued_at._datetime if parsed.issued_at else None
    if issued is None or now - issued > MAX_ISSUED_AT_AGE or issued - now > MAX_CLOCK_SKEW:
        raise _invalid("issued-at missing or outside the allowed window")
    if parsed.expiration_time is None:
        raise _invalid("expiration-time is required")

    stored = await redis.getdel(_nonce_key(parsed.nonce))
    if stored is None:
        raise _invalid("unknown, expired or already-used nonce")

    provider = HTTPProvider(settings.rpc_url)  # enables EIP-1271 for contract wallets

    def _verify() -> None:
        parsed.verify(signature, domain=settings.siwe_domain, nonce=parsed.nonce, provider=provider)

    try:
        await asyncio.to_thread(_verify)
    except VerificationError as exc:
        raise _invalid(f"signature verification failed: {type(exc).__name__}") from exc
    except Exception as exc:  # RPC down while checking a contract wallet, bad hex, etc.
        raise _invalid("signature verification failed") from exc
    return str(parsed.address).lower()
