"""FastAPI dependencies: the authenticated user and the admin gate."""

from typing import Annotated

import redis.asyncio as aioredis
from fastapi import Depends, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.tokens import TokenClaims, decode_token, is_revoked
from app.core.config import Settings
from app.core.db import get_session
from app.core.errors import AppError
from app.core.ratelimit import hit
from app.models import User, UserRole

SessionDep = Annotated[AsyncSession, Depends(get_session)]


def _unauthorized(message: str = "authentication required") -> AppError:
    return AppError(401, "unauthorized", message)


async def current_claims(request: Request) -> TokenClaims:
    settings: Settings = request.app.state.settings
    redis: aioredis.Redis = request.app.state.redis
    token = request.cookies.get(settings.session_cookie_name)
    if not token:
        raise _unauthorized()
    claims = decode_token(settings, token)
    if claims is None:
        raise _unauthorized("invalid or expired session")
    if await is_revoked(redis, claims.jti):
        raise _unauthorized("session was logged out")
    return claims


ClaimsDep = Annotated[TokenClaims, Depends(current_claims)]


async def current_user(request: Request, claims: ClaimsDep, session: SessionDep) -> User:
    settings: Settings = request.app.state.settings
    redis: aioredis.Redis = request.app.state.redis
    user = await session.scalar(select(User).where(User.address == claims.address))
    if user is None:
        raise _unauthorized("unknown user")
    await hit(
        redis, f"user:{user.id}", settings.rate_limit_user, settings.rate_limit_window_seconds
    )
    return user


UserDep = Annotated[User, Depends(current_user)]


async def require_admin(user: UserDep) -> User:
    if user.role != UserRole.ADMIN:
        raise AppError(403, "forbidden", "admin role required")
    return user


AdminDep = Annotated[User, Depends(require_admin)]
