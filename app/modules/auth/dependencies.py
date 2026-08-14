"""Auth dependencies: bearer-token verification and role-based access control.

`admin` is always treated as a superuser and is implicitly allowed wherever
any other role is required — CLAUDE.md defines four roles precisely so admin
can act as the top of that hierarchy; call sites don't need to remember to
list it every time.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from typing import Annotated

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.modules.auth.db import get_db_session
from app.modules.auth.models import User
from app.modules.auth.schemas import CurrentUser, Role
from app.modules.auth.security import TokenError, decode_token

_bearer_scheme = HTTPBearer(auto_error=True)


async def get_current_user(
    request: Request,
    credentials: Annotated[HTTPAuthorizationCredentials, Depends(_bearer_scheme)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> CurrentUser:
    secret: str = request.app.state.jwt_secret
    try:
        claims = decode_token(credentials.credentials, secret, settings.jwt_algorithm, "access")
    except TokenError as exc:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=str(exc)) from exc

    try:
        user_id = uuid.UUID(claims["sub"])
    except (KeyError, ValueError) as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Token subject is invalid"
        ) from exc

    user = await session.get(User, user_id)
    if user is None or not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="User not found or inactive"
        )

    return CurrentUser(
        id=str(user.id),
        email=user.email,
        role=user.role,
        tenant_id=user.tenant_id,
        is_active=user.is_active,
    )


def require_role(*roles: Role) -> Callable[[CurrentUser], Awaitable[CurrentUser]]:
    allowed = {"admin", *roles}

    async def _dependency(
        current_user: Annotated[CurrentUser, Depends(get_current_user)],
    ) -> CurrentUser:
        if current_user.role not in allowed:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Role {current_user.role!r} is not permitted to perform this action",
            )
        return current_user

    return _dependency
