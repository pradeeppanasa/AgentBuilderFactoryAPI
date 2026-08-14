"""Password hashing and JWT issuing/verification.

Access and refresh tokens are both plain JWTs signed with the same secret
(fetched from Secrets Manager at startup, see app/main.py), distinguished by
a `type` claim so one can never be used in place of the other. There is no
server-side refresh-token revocation list in this phase — refresh is
stateless, matching the access token's trust model.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any, Literal

import bcrypt
import jwt

from app.modules.auth.models import User

TokenType = Literal["access", "refresh"]


class TokenError(Exception):
    """Raised when a bearer token is missing, malformed, expired, or the wrong type."""


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verify_password(password: str, hashed_password: str) -> bool:
    return bcrypt.checkpw(password.encode("utf-8"), hashed_password.encode("utf-8"))


def _issue_token(
    *,
    user: User,
    token_type: TokenType,
    secret: str,
    algorithm: str,
    expires_delta: timedelta,
) -> str:
    now = datetime.now(UTC)
    payload = {
        "sub": str(user.id),
        "tenant_id": user.tenant_id,
        "role": user.role,
        "type": token_type,
        "iat": now,
        "exp": now + expires_delta,
    }
    return jwt.encode(payload, secret, algorithm=algorithm)


def create_access_token(user: User, secret: str, algorithm: str, expire_minutes: int) -> str:
    return _issue_token(
        user=user,
        token_type="access",
        secret=secret,
        algorithm=algorithm,
        expires_delta=timedelta(minutes=expire_minutes),
    )


def create_refresh_token(user: User, secret: str, algorithm: str, expire_days: int) -> str:
    return _issue_token(
        user=user,
        token_type="refresh",
        secret=secret,
        algorithm=algorithm,
        expires_delta=timedelta(days=expire_days),
    )


def decode_token(
    token: str, secret: str, algorithm: str, expected_type: TokenType
) -> dict[str, Any]:
    try:
        claims: dict[str, Any] = jwt.decode(token, secret, algorithms=[algorithm])
    except jwt.ExpiredSignatureError as exc:
        raise TokenError("Token has expired") from exc
    except jwt.InvalidTokenError as exc:
        raise TokenError(f"Invalid token: {exc}") from exc

    if claims.get("type") != expected_type:
        raise TokenError(f"Expected a {expected_type!r} token, got {claims.get('type')!r}")
    return claims
