"""Unified auth layer (Sprint 3 Phase 1 — CLAUDE.md Section 64.1, S-01/S-04
foundation, R67).

Adapted from the CLAUDE.md skeleton to this runtime's actual shape: one
container always serves exactly one (tenant_id, agent_id) pair — AGENT_ID/
TENANT_ID env vars, config_loader.py — not a shared multi-agent endpoint
routed by an incoming agent_id path parameter. So instead of a
`get_agent_from_db(agent_id)` helper that looks a record up by whatever the
caller claims, every provider here is handed the freshly-fetched record for
THIS container's own agent (config_loader.get_current_agent_record(),
called by main.py's auth_middleware on every request — R67's "DB is the
authority, not the env var" still holds: it is re-fetched every request,
never read from the startup-cached agent_config).

Replaces the old main.py `_check_auth`/AGENT_API_KEY placeholder entirely —
that compared against a single value pulled from a plain environment
variable with no rotation, no revocation, and no per-agent secret. Real
credentials now live only in Secrets Manager (R55/R60/R61); DynamoDB (the
agent record) holds ARNs only.
"""

from __future__ import annotations

import hmac
import time
from dataclasses import dataclass, field
from typing import Any

import boto3

# Replaces an indefinite @lru_cache on purpose (CLAUDE.md Phase 1 "What not
# to do") — an lru_cache never expires, so a rotated or revoked key would
# keep validating against the stale cached value for the life of the
# process. 5 minutes matches Section 64.4's rotation-grace design: within
# that window a caller may still see the old key accepted after a rotation,
# which is the intended behaviour during the 24h dual-key grace period, not
# a bug — it is not intended after a revocation, which is why
# ApiKeyAuthProvider checks agent_record["api_key_revoked"] fresh from the
# DB (never cached) on every call, ahead of any secret-value comparison.
_CACHE_TTL_SECONDS = 300
_key_cache: dict[str, tuple[str, float]] = {}

_secrets_client: Any | None = None


def _get_secrets_client() -> Any:
    global _secrets_client
    if _secrets_client is None:
        _secrets_client = boto3.client("secretsmanager")
    return _secrets_client


def _get_secret_value(secret_arn: str) -> str:
    cached = _key_cache.get(secret_arn)
    if cached is not None and (time.monotonic() - cached[1]) < _CACHE_TTL_SECONDS:
        return cached[0]
    value: str = _get_secrets_client().get_secret_value(SecretId=secret_arn)["SecretString"]
    _key_cache[secret_arn] = (value, time.monotonic())
    return value


@dataclass
class AuthContext:
    tenant_id: str
    agent_id: str
    principal_id: str
    scopes: list[str] = field(default_factory=lambda: ["agent:invoke"])
    auth_method: str = "api_key"


class AuthProvider:
    """Returns an AuthContext if this provider validated the credential,
    None if it doesn't recognise the credential shape at all — so the next
    provider in the chain gets a turn. None is "not mine to judge", not
    "rejected"; AuthChain treats every provider returning None as a final
    401, so a provider that DOES recognise its own shape but finds the
    credential invalid must also return None (there is no separate
    "recognised but rejected" signal — the caller never learns which
    provider almost matched, which is the correct amount of information to
    leak to a failed, unauthenticated caller)."""

    async def authenticate(
        self, authorization: str, agent_record: dict[str, Any]
    ) -> AuthContext | None:
        raise NotImplementedError


class ApiKeyAuthProvider(AuthProvider):
    """Validates a Bearer API key against Secrets Manager. Supports the
    dual-key rotation grace period (Section 64.4/Phase 8): both the
    current and, for a limited window, the previous key are accepted."""

    async def authenticate(
        self, authorization: str, agent_record: dict[str, Any]
    ) -> AuthContext | None:
        if not authorization.startswith("Bearer "):
            return None
        provided = authorization.removeprefix("Bearer ").strip()
        if not provided:
            return None

        # Revocation is checked before any secret is even fetched, and
        # against the just-fetched record, never a cached one (R60/Phase 8
        # "revoked key returns 401 within 5-min cache TTL" — the *key
        # value* cache has that TTL; the revoked flag itself is read fresh
        # every request via config_loader.get_current_agent_record()).
        if agent_record.get("api_key_revoked"):
            return None

        secret_arn = agent_record.get("api_key_secret_arn")
        if not secret_arn:
            # No key provisioned for this agent yet — default deny, never
            # an open door just because nothing is configured.
            return None

        if hmac.compare_digest(provided, _get_secret_value(secret_arn)):
            return self._context(agent_record)

        prev_arn = agent_record.get("previous_api_key_secret_arn")
        prev_expires_at = agent_record.get("previous_key_expires_at") or 0
        if (
            prev_arn
            and time.time() < prev_expires_at
            and hmac.compare_digest(provided, _get_secret_value(prev_arn))
        ):
            return self._context(agent_record)

        return None

    @staticmethod
    def _context(agent_record: dict[str, Any]) -> AuthContext:
        return AuthContext(
            tenant_id=agent_record["tenant_id"],
            agent_id=agent_record["agent_id"],
            principal_id="api_key_caller",
            scopes=["agent:invoke"],
            auth_method="api_key",
        )


class JwtAuthProvider(AuthProvider):
    """Stub only — real JWT/OAuth2 validation is S-12 (P2, CLAUDE.md
    Section 63.1). Recognises the token shape (a compact JWT's base64url
    header always starts "eyJ") purely so a JWT-shaped credential fails
    the same way today as it will once this is implemented for real,
    rather than silently falling through to ApiKeyAuthProvider's
    Bearer-token check and failing for an unrelated reason."""

    async def authenticate(
        self, authorization: str, agent_record: dict[str, Any]
    ) -> AuthContext | None:
        if not authorization.startswith("Bearer ey"):
            return None
        # TODO (S-12): validate JWT signature, expiry, issuer; extract
        # tenant_id/agent_id/scopes from claims and return a real
        # AuthContext. Until then this always defers (returns None).
        return None


class AuthChain:
    """Tries each provider in order; first match wins. Returns None (never
    raises) when nothing matches — main.py's auth_middleware decides the
    HTTP response, since an HTTPException raised from inside a
    `@app.middleware("http")` function is NOT caught by Starlette's
    ExceptionMiddleware (that wraps INSIDE user middleware, not outside)
    and would surface as an unhandled 500 rather than a 401."""

    def __init__(self, providers: list[AuthProvider]) -> None:
        self._providers = providers

    async def authenticate(
        self, authorization: str, agent_record: dict[str, Any]
    ) -> AuthContext | None:
        for provider in self._providers:
            ctx = await provider.authenticate(authorization, agent_record)
            if ctx is not None:
                return ctx
        return None


# Singleton, instantiated once at import time (main.py imports this name
# directly rather than constructing its own chain).
auth_chain = AuthChain([ApiKeyAuthProvider(), JwtAuthProvider()])
