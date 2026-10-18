"""SIWE login, session cookie and logout."""

import time
from datetime import UTC, datetime

import redis.asyncio as aioredis
from fastapi import APIRouter, Depends, Request, Response
from sqlalchemy import select

from app.auth.deps import ClaimsDep, SessionDep, UserDep
from app.auth.siwe_login import create_nonce, verify_login
from app.auth.tokens import issue_token, revoke
from app.core.config import Settings
from app.core.ratelimit import ip_limit
from app.models import User, UserRole
from app.schemas.api import LoginIn, NonceOut, UserOut
from app.schemas.common import checksum

router = APIRouter(prefix="/auth", tags=["auth"])
AuthLimit = Depends(ip_limit("auth", "rate_limit_auth"))


def _user_out(user: User) -> UserOut:
    return UserOut(id=user.id, address=checksum(user.address), role=user.role.value)


@router.get("/nonce", response_model=NonceOut, dependencies=[AuthLimit])
async def get_nonce(request: Request) -> NonceOut:
    settings: Settings = request.app.state.settings
    redis: aioredis.Redis = request.app.state.redis
    return NonceOut(
        nonce=await create_nonce(redis, settings), expires_in=settings.siwe_nonce_ttl_seconds
    )


@router.post("/login", response_model=UserOut, dependencies=[AuthLimit])
async def login(
    body: LoginIn, request: Request, response: Response, session: SessionDep
) -> UserOut:
    settings: Settings = request.app.state.settings
    redis: aioredis.Redis = request.app.state.redis
    address = await verify_login(redis, settings, body.message, body.signature)

    role = UserRole.ADMIN if address in settings.admin_address_set else UserRole.USER
    user = await session.scalar(select(User).where(User.address == address))
    if user is None:
        user = User(address=address, role=role)
        session.add(user)
    user.role = role  # the env allow-list is authoritative on every login
    user.last_login_at = datetime.now(UTC)
    await session.commit()

    token, claims = issue_token(settings, address)
    response.set_cookie(
        settings.session_cookie_name,
        token,
        max_age=claims.expires_at - int(time.time()),
        httponly=True,
        secure=settings.session_cookie_secure,
        samesite="lax",
        path="/",
    )
    return _user_out(user)


@router.post("/logout", status_code=204)
async def logout(request: Request, claims: ClaimsDep) -> Response:
    """Revoke the presented token (Redis denylist) and clear the cookie."""
    settings: Settings = request.app.state.settings
    redis: aioredis.Redis = request.app.state.redis
    await revoke(redis, claims)
    response = Response(status_code=204)
    response.delete_cookie(
        settings.session_cookie_name,
        path="/",
        httponly=True,
        secure=settings.session_cookie_secure,
        samesite="lax",
    )
    return response


@router.get("/me", response_model=UserOut)
async def me(user: UserDep) -> UserOut:
    return _user_out(user)
