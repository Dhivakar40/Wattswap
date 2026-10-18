"""JWT session tokens and the Redis revocation denylist."""

import secrets
import time
from dataclasses import dataclass
from typing import Any

import jwt
import redis.asyncio as aioredis

from app.core.config import Settings

ALGORITHM = "HS256"
ISSUER = "wattswap-api"


@dataclass(frozen=True)
class TokenClaims:
    address: str
    jti: str
    expires_at: int


def issue_token(
    settings: Settings, address: str, *, now: int | None = None
) -> tuple[str, TokenClaims]:
    issued = int(time.time()) if now is None else now
    claims = TokenClaims(address.lower(), secrets.token_hex(16), issued + settings.jwt_ttl_seconds)
    payload: dict[str, Any] = {
        "iss": ISSUER,
        "sub": claims.address,
        "jti": claims.jti,
        "iat": issued,
        "exp": claims.expires_at,
    }
    token = jwt.encode(payload, settings.jwt_secret.get_secret_value(), algorithm=ALGORITHM)
    return token, claims


def decode_token(settings: Settings, token: str) -> TokenClaims | None:
    """Verified claims, or None for any invalid/expired/tampered token."""
    try:
        data = jwt.decode(
            token,
            settings.jwt_secret.get_secret_value(),
            algorithms=[ALGORITHM],  # pin the algorithm; never trust the header
            issuer=ISSUER,
            options={"require": ["exp", "iat", "sub", "jti", "iss"]},
        )
    except jwt.PyJWTError:
        return None
    return TokenClaims(str(data["sub"]), str(data["jti"]), int(data["exp"]))


def _deny_key(jti: str) -> str:
    return f"jwt:deny:{jti}"


async def revoke(redis: aioredis.Redis, claims: TokenClaims, *, now: int | None = None) -> None:
    """Denylist a token until it would have expired anyway."""
    current = int(time.time()) if now is None else now
    ttl = max(claims.expires_at - current, 1)
    await redis.set(_deny_key(claims.jti), "1", ex=ttl)


async def is_revoked(redis: aioredis.Redis, jti: str) -> bool:
    return bool(await redis.exists(_deny_key(jti)))
