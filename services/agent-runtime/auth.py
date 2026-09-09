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

import asyncio
import hmac
import json
import time
import urllib.request
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, cast

import boto3
import jwt
import structlog

if TYPE_CHECKING:
    from cryptography.hazmat.primitives.asymmetric.rsa import RSAPublicKey

logger = structlog.get_logger()

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


# Sprint 4 Phase 4 (S-12) — same TTL-cache convention as _key_cache above,
# a second dict rather than reusing that one since the values are JWKS
# documents (dict), not secret strings, and the cache key is a JWKS URL,
# not a Secrets Manager ARN. Module-level function (not a class/injectable
# client) matching _get_secret_value's own shape, so tests can monkeypatch
# it exactly the same way they already monkeypatch _get_secrets_client.
_JWKS_CACHE_TTL_SECONDS = 300
_jwks_cache: dict[str, tuple[dict[str, Any], float]] = {}


def _fetch_jwks(jwks_url: str) -> dict[str, Any]:
    """Synchronous by design — called via asyncio.to_thread() from
    JwtAuthProvider so the blocking urllib call never stalls the event
    loop, matching this codebase's established pattern for blocking I/O
    inside async methods (e.g. tool_executor.py's Lambda invoke,
    hitl.py's DynamoDB put_item)."""
    cached = _jwks_cache.get(jwks_url)
    if cached is not None and (time.monotonic() - cached[1]) < _JWKS_CACHE_TTL_SECONDS:
        return cached[0]
    with urllib.request.urlopen(jwks_url, timeout=5) as response:  # noqa: S310
        jwks: dict[str, Any] = json.loads(response.read())
    _jwks_cache[jwks_url] = (jwks, time.monotonic())
    return jwks


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
    """Sprint 4 Phase 4 (S-12, CLAUDE.md Section 63.1) — validates a JWT
    Bearer token issued by the CUSTOMER's OWN OAuth2/OIDC identity
    provider. A completely different trust root from the Factory
    Console's own single-secret HS256 user auth (app/modules/auth/ in the
    Factory Runtime, which signs and verifies its own tokens against one
    shared `jwt_secret_arn`) — this provider never signs anything; it only
    verifies a signature against public keys fetched from the CUSTOMER's
    own JWKS endpoint. The two systems share no code and no trust root.

    Per-agent trust config (issuer, audience, JWKS URL, tenant-claim name)
    lives as flat fields directly on AgentRecord — the same panasa-agents
    table config_loader.get_current_agent_record() already reads on every
    request, matching the api_key_secret_arn-style precedent.
    AgentConfiguration (a different table, loaded once at startup) is
    deliberately not consulted here, same reasoning as ApiKeyAuthProvider.
    An agent with none of these fields set has JWT auth off entirely —
    this provider always defers (returns None), never partially validates.

    Scope, deliberately: RS256 only (the default for essentially every
    major OIDC provider — Okta, Auth0, Azure AD, Google, Cognito). No
    ES256/other JWA algorithms — a real future improvement, not built
    here. `audience` is optional (only enforced if the agent record sets
    one); `issuer` and a resolvable JWKS key are always required.

    Every failure path returns None (never raises) — a security control,
    so it fails CLOSED (R39): an unreachable JWKS endpoint, an unknown
    `kid`, a malformed key, an expired/wrong-issuer/wrong-audience/
    bad-signature token, or a missing tenant claim all result in the same
    401 as an unrecognised credential, with the specific reason only in
    this process's own logs, never in the response body."""

    async def authenticate(
        self, authorization: str, agent_record: dict[str, Any]
    ) -> AuthContext | None:
        if not authorization.startswith("Bearer ey"):
            return None
        token = authorization.removeprefix("Bearer ").strip()

        jwks_url = agent_record.get("jwt_jwks_url")
        issuer = agent_record.get("jwt_issuer")
        if not jwks_url or not issuer:
            return None
        if not jwks_url.startswith("https://"):
            # Every real OIDC JWKS endpoint is HTTPS; a misconfigured
            # http:// value is a config error, not something to attempt.
            logger.warning("jwt_jwks_url_not_https", jwks_url=jwks_url)
            return None

        try:
            unverified_header = jwt.get_unverified_header(token)
        except jwt.PyJWTError:
            return None
        kid = unverified_header.get("kid")

        try:
            jwks = await asyncio.to_thread(_fetch_jwks, jwks_url)
        except Exception as exc:
            logger.warning("jwt_jwks_fetch_failed", jwks_url=jwks_url, error=str(exc))
            return None

        key_data = next(
            (k for k in jwks.get("keys", []) if k.get("kid") == kid),
            None,
        )
        if key_data is None:
            return None

        try:
            # from_jwk()'s return type also covers RSAPrivateKey (the same
            # method parses private JWKs too); a JWKS document only ever
            # contains public keys, so this cast reflects a real guarantee
            # the type checker can't see, not a workaround.
            public_key = cast(
                "RSAPublicKey", jwt.algorithms.RSAAlgorithm.from_jwk(json.dumps(key_data))
            )
            audience = agent_record.get("jwt_audience")
            options: dict[str, Any] = {"require": ["exp", "iat"]}
            if audience is None:
                # PyJWT rejects a token that carries an `aud` claim unless
                # an `audience` to check it against was also given — an
                # unconfigured audience must mean "don't check", not "no
                # token with an aud claim can ever pass".
                options["verify_aud"] = False
            claims = jwt.decode(
                token,
                key=public_key,
                algorithms=["RS256"],
                issuer=issuer,
                audience=audience,
                leeway=30,
                options=options,
            )
        except Exception as exc:
            # Broad on purpose: PyJWTError covers expired/wrong-issuer/
            # wrong-audience/bad-signature tokens, but a malformed JWKS
            # key entry can also raise a plain ValueError out of
            # from_jwk() — both must fail closed the same way.
            logger.warning("jwt_validation_failed", error=str(exc))
            return None

        tenant_claim = agent_record.get("jwt_tenant_claim") or "tenant_id"
        tenant_id = claims.get(tenant_claim)
        if not tenant_id:
            logger.warning("jwt_missing_tenant_claim", tenant_claim=tenant_claim)
            return None

        scope_claim = claims.get("scope")
        if isinstance(scope_claim, str):
            scopes = scope_claim.split()
        elif isinstance(claims.get("scopes"), list):
            scopes = claims["scopes"]
        else:
            scopes = ["agent:invoke"]

        return AuthContext(
            tenant_id=str(tenant_id),
            agent_id=agent_record["agent_id"],
            principal_id=str(claims.get("sub") or claims.get("client_id") or "jwt_caller"),
            scopes=scopes,
            auth_method="jwt",
        )


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
