"""Unit tests for the Sprint 3 Phase 1 auth layer (CLAUDE.md Section 64.1).

Exercises ApiKeyAuthProvider/JwtAuthProvider/AuthChain directly, decoupled
from FastAPI — the HTTP-level 401/403 behaviour (auth_middleware) is
covered separately in tests/test_main.py.
"""

from __future__ import annotations

import json
import time
import urllib.request
from typing import Any

import auth as auth_module
import jwt
import pytest
from auth import ApiKeyAuthProvider, AuthChain, JwtAuthProvider
from cryptography.hazmat.primitives.asymmetric import rsa


class _FakeSecretsClient:
    def __init__(self, values: dict[str, str]) -> None:
        self._values = values
        self.calls: list[str] = []

    def get_secret_value(self, SecretId: str) -> dict[str, str]:
        self.calls.append(SecretId)
        return {"SecretString": self._values[SecretId]}


@pytest.fixture(autouse=True)
def _reset_key_cache() -> None:
    auth_module._key_cache.clear()
    yield
    auth_module._key_cache.clear()


def _patch_secrets(monkeypatch: pytest.MonkeyPatch, values: dict[str, str]) -> _FakeSecretsClient:
    fake = _FakeSecretsClient(values)
    monkeypatch.setattr(auth_module, "_get_secrets_client", lambda: fake)
    return fake


_CURRENT_ARN = "arn:aws:secretsmanager:eu-west-2:123456789012:secret:key-current"
_PREVIOUS_ARN = "arn:aws:secretsmanager:eu-west-2:123456789012:secret:key-previous"


def _agent_record(**overrides: Any) -> dict[str, Any]:
    defaults: dict[str, Any] = {
        "tenant_id": "tenant-a",
        "agent_id": "faq-agent-1",
        "api_key_secret_arn": _CURRENT_ARN,
    }
    defaults.update(overrides)
    return defaults


# ── Sprint 4 Phase 4 (S-12) — JwtAuthProvider, real RS256/JWKS ──────────

_PRIVATE_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
_PUBLIC_KEY = _PRIVATE_KEY.public_key()
_KID = "test-key-1"
_JWKS_URL = "https://idp.example.com/.well-known/jwks.json"
_ISSUER = "https://idp.example.com/"
_AUDIENCE = "panasa-agent-runtime"


@pytest.fixture(autouse=True)
def _reset_jwks_cache() -> None:
    auth_module._jwks_cache.clear()
    yield
    auth_module._jwks_cache.clear()


def _jwks_document() -> dict[str, Any]:
    jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(_PUBLIC_KEY))
    jwk["kid"] = _KID
    jwk["use"] = "sig"
    jwk["alg"] = "RS256"
    return {"keys": [jwk]}


def _patch_jwks(monkeypatch: pytest.MonkeyPatch, jwks: dict[str, Any] | None = None) -> None:
    monkeypatch.setattr(auth_module, "_fetch_jwks", lambda url: jwks or _jwks_document())


def _make_token(
    claims: dict[str, Any],
    *,
    kid: str = _KID,
    key: Any = _PRIVATE_KEY,
    issuer: str = _ISSUER,
    audience: str = _AUDIENCE,
    issued_at_offset: int = 0,
    expires_in: int = 300,
) -> str:
    now = int(time.time()) + issued_at_offset
    payload = {"iss": issuer, "aud": audience, "iat": now, "exp": now + expires_in, **claims}
    return jwt.encode(payload, key, algorithm="RS256", headers={"kid": kid})


def _jwt_agent_record(**overrides: Any) -> dict[str, Any]:
    defaults: dict[str, Any] = {
        "tenant_id": "tenant-a",
        "agent_id": "faq-agent-1",
        "jwt_issuer": _ISSUER,
        "jwt_audience": _AUDIENCE,
        "jwt_jwks_url": _JWKS_URL,
    }
    defaults.update(overrides)
    return defaults


async def test_jwt_provider_accepts_valid_token(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_jwks(monkeypatch)
    token = _make_token({"tenant_id": "tenant-a", "sub": "client-123"})
    provider = JwtAuthProvider()

    ctx = await provider.authenticate(f"Bearer {token}", _jwt_agent_record())

    assert ctx is not None
    assert ctx.tenant_id == "tenant-a"
    assert ctx.agent_id == "faq-agent-1"
    assert ctx.principal_id == "client-123"
    assert ctx.auth_method == "jwt"
    assert ctx.scopes == ["agent:invoke"]


async def test_jwt_provider_extracts_space_separated_scope_claim(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_jwks(monkeypatch)
    token = _make_token({"tenant_id": "tenant-a", "scope": "agent:invoke agent:admin"})
    provider = JwtAuthProvider()

    ctx = await provider.authenticate(f"Bearer {token}", _jwt_agent_record())

    assert ctx is not None
    assert ctx.scopes == ["agent:invoke", "agent:admin"]


async def test_jwt_provider_returns_claim_tenant_id_even_when_it_mismatches_agent_record(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """R67 — this provider returns exactly what the token claims, never
    silently substitutes the agent record's own tenant_id; main.py's
    auth_middleware is what catches the mismatch and 403s (see
    test_main.py's test_chat_tenant_mismatch_returns_403_and_writes_audit_
    event, which exercises that same path with a fake auth chain)."""
    _patch_jwks(monkeypatch)
    token = _make_token({"tenant_id": "some-other-tenant"})
    provider = JwtAuthProvider()

    ctx = await provider.authenticate(f"Bearer {token}", _jwt_agent_record())

    assert ctx is not None
    assert ctx.tenant_id == "some-other-tenant"


async def test_jwt_provider_rejects_http_jwks_url(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_jwks(monkeypatch)
    token = _make_token({"tenant_id": "tenant-a"})
    provider = JwtAuthProvider()
    record = _jwt_agent_record(jwt_jwks_url="http://idp.example.com/jwks.json")

    ctx = await provider.authenticate(f"Bearer {token}", record)

    assert ctx is None


async def test_jwt_provider_rejects_expired_token(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_jwks(monkeypatch)
    token = _make_token({"tenant_id": "tenant-a"}, issued_at_offset=-3600, expires_in=1800)
    provider = JwtAuthProvider()

    ctx = await provider.authenticate(f"Bearer {token}", _jwt_agent_record())

    assert ctx is None


async def test_jwt_provider_rejects_wrong_issuer(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_jwks(monkeypatch)
    token = _make_token(
        {"tenant_id": "tenant-a"}, issuer="https://not-the-configured-issuer.example.com/"
    )
    provider = JwtAuthProvider()

    ctx = await provider.authenticate(f"Bearer {token}", _jwt_agent_record())

    assert ctx is None


async def test_jwt_provider_rejects_wrong_audience(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_jwks(monkeypatch)
    token = _make_token({"tenant_id": "tenant-a"}, audience="some-other-audience")
    provider = JwtAuthProvider()

    ctx = await provider.authenticate(f"Bearer {token}", _jwt_agent_record())

    assert ctx is None


async def test_jwt_provider_accepts_any_audience_when_none_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_jwks(monkeypatch)
    token = _make_token({"tenant_id": "tenant-a"}, audience="whatever")
    provider = JwtAuthProvider()
    record = _jwt_agent_record()
    del record["jwt_audience"]

    ctx = await provider.authenticate(f"Bearer {token}", record)

    assert ctx is not None


async def test_jwt_provider_rejects_signature_from_a_different_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_jwks(monkeypatch)
    other_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    token = _make_token({"tenant_id": "tenant-a"}, key=other_key)
    provider = JwtAuthProvider()

    ctx = await provider.authenticate(f"Bearer {token}", _jwt_agent_record())

    assert ctx is None


async def test_jwt_provider_rejects_unknown_kid(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_jwks(monkeypatch)
    token = _make_token({"tenant_id": "tenant-a"}, kid="some-other-kid")
    provider = JwtAuthProvider()

    ctx = await provider.authenticate(f"Bearer {token}", _jwt_agent_record())

    assert ctx is None


async def test_jwt_provider_rejects_when_jwks_fetch_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom(url: str) -> dict[str, Any]:
        raise ConnectionError("jwks endpoint unreachable")

    monkeypatch.setattr(auth_module, "_fetch_jwks", _boom)
    token = _make_token({"tenant_id": "tenant-a"})
    provider = JwtAuthProvider()

    ctx = await provider.authenticate(f"Bearer {token}", _jwt_agent_record())

    assert ctx is None


async def test_jwt_provider_rejects_missing_tenant_claim(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_jwks(monkeypatch)
    token = _make_token({})
    provider = JwtAuthProvider()

    ctx = await provider.authenticate(f"Bearer {token}", _jwt_agent_record())

    assert ctx is None


async def test_jwt_provider_honours_custom_tenant_claim_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_jwks(monkeypatch)
    token = _make_token({"org_id": "tenant-a"})
    provider = JwtAuthProvider()
    record = _jwt_agent_record(jwt_tenant_claim="org_id")

    ctx = await provider.authenticate(f"Bearer {token}", record)

    assert ctx is not None
    assert ctx.tenant_id == "tenant-a"


async def test_auth_chain_falls_through_to_jwt_provider_when_api_key_not_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_jwks(monkeypatch)
    token = _make_token({"tenant_id": "tenant-a"})
    chain = AuthChain([ApiKeyAuthProvider(), JwtAuthProvider()])
    record = _jwt_agent_record()  # no api_key_secret_arn at all

    ctx = await chain.authenticate(f"Bearer {token}", record)

    assert ctx is not None
    assert ctx.auth_method == "jwt"


class _FakeHTTPResponse:
    def __init__(self, data: bytes) -> None:
        self._data = data

    def read(self) -> bytes:
        return self._data

    def __enter__(self) -> _FakeHTTPResponse:
        return self

    def __exit__(self, *args: object) -> None:
        return None


def test_jwks_cache_avoids_refetching_within_ttl(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    def _fake_urlopen(url: str, timeout: int = 5) -> _FakeHTTPResponse:
        calls.append(url)
        return _FakeHTTPResponse(json.dumps({"keys": []}).encode("utf-8"))

    monkeypatch.setattr(urllib.request, "urlopen", _fake_urlopen)

    first = auth_module._fetch_jwks(_JWKS_URL)
    second = auth_module._fetch_jwks(_JWKS_URL)

    assert first == second == {"keys": []}
    assert calls == [_JWKS_URL]


def test_jwks_cache_refetches_after_ttl_expires(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    def _fake_urlopen(url: str, timeout: int = 5) -> _FakeHTTPResponse:
        calls.append(url)
        return _FakeHTTPResponse(json.dumps({"keys": []}).encode("utf-8"))

    monkeypatch.setattr(urllib.request, "urlopen", _fake_urlopen)
    monkeypatch.setattr(auth_module, "_JWKS_CACHE_TTL_SECONDS", 0)

    auth_module._fetch_jwks(_JWKS_URL)
    auth_module._fetch_jwks(_JWKS_URL)

    assert calls == [_JWKS_URL, _JWKS_URL]


async def test_api_key_provider_accepts_correct_key(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_secrets(monkeypatch, {_CURRENT_ARN: "sk-correct"})
    provider = ApiKeyAuthProvider()

    ctx = await provider.authenticate("Bearer sk-correct", _agent_record())

    assert ctx is not None
    assert ctx.tenant_id == "tenant-a"
    assert ctx.agent_id == "faq-agent-1"
    assert ctx.auth_method == "api_key"
    assert ctx.scopes == ["agent:invoke"]


async def test_api_key_provider_rejects_wrong_key(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_secrets(monkeypatch, {_CURRENT_ARN: "sk-correct"})
    provider = ApiKeyAuthProvider()

    ctx = await provider.authenticate("Bearer wrong", _agent_record())

    assert ctx is None


async def test_api_key_provider_defers_on_non_bearer_authorization(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_secrets(monkeypatch, {})
    provider = ApiKeyAuthProvider()

    assert await provider.authenticate("", _agent_record()) is None
    assert await provider.authenticate("Basic dXNlcjpwYXNz", _agent_record()) is None
    assert await provider.authenticate("Bearer ", _agent_record()) is None


async def test_api_key_provider_denies_when_no_key_provisioned(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_secrets(monkeypatch, {})
    provider = ApiKeyAuthProvider()
    record = _agent_record()
    del record["api_key_secret_arn"]

    ctx = await provider.authenticate("Bearer anything", record)

    assert ctx is None


async def test_api_key_provider_denies_revoked_key_even_if_correct(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_secrets(monkeypatch, {_CURRENT_ARN: "sk-correct"})
    provider = ApiKeyAuthProvider()

    ctx = await provider.authenticate("Bearer sk-correct", _agent_record(api_key_revoked=True))

    assert ctx is None


async def test_api_key_provider_accepts_previous_key_during_grace_period(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_secrets(monkeypatch, {_CURRENT_ARN: "sk-new", _PREVIOUS_ARN: "sk-old"})
    record = _agent_record(
        previous_api_key_secret_arn=_PREVIOUS_ARN,
        previous_key_expires_at=time.time() + 3600,
    )
    provider = ApiKeyAuthProvider()

    ctx = await provider.authenticate("Bearer sk-old", record)

    assert ctx is not None
    assert ctx.tenant_id == "tenant-a"


async def test_api_key_provider_rejects_previous_key_after_grace_period_expires(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_secrets(monkeypatch, {_CURRENT_ARN: "sk-new", _PREVIOUS_ARN: "sk-old"})
    record = _agent_record(
        previous_api_key_secret_arn=_PREVIOUS_ARN,
        previous_key_expires_at=time.time() - 10,
    )
    provider = ApiKeyAuthProvider()

    ctx = await provider.authenticate("Bearer sk-old", record)

    assert ctx is None


async def test_api_key_provider_ignores_previous_key_when_not_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_secrets(monkeypatch, {_CURRENT_ARN: "sk-new"})
    provider = ApiKeyAuthProvider()

    ctx = await provider.authenticate("Bearer sk-old-value-nobody-configured", _agent_record())

    assert ctx is None


async def test_jwt_provider_defers_when_agent_has_no_jwt_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An agent with none of jwt_issuer/jwt_jwks_url set has JWT auth off
    entirely — this must defer before ever trying to parse the token, so
    its validity doesn't matter here."""
    provider = JwtAuthProvider()

    ctx = await provider.authenticate("Bearer eyJhbGciOiJIUzI1NiJ9.fake.sig", _agent_record())

    assert ctx is None


async def test_jwt_provider_defers_on_non_jwt_shaped_bearer_token() -> None:
    provider = JwtAuthProvider()

    ctx = await provider.authenticate("Bearer sk-plain-api-key", _agent_record())

    assert ctx is None


async def test_auth_chain_returns_none_when_no_provider_matches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_secrets(monkeypatch, {_CURRENT_ARN: "sk-correct"})
    chain = AuthChain([ApiKeyAuthProvider(), JwtAuthProvider()])

    ctx = await chain.authenticate("Bearer nope", _agent_record())

    assert ctx is None


async def test_auth_chain_returns_first_matching_provider_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_secrets(monkeypatch, {_CURRENT_ARN: "sk-correct"})
    chain = AuthChain([ApiKeyAuthProvider(), JwtAuthProvider()])

    ctx = await chain.authenticate("Bearer sk-correct", _agent_record())

    assert ctx is not None
    assert ctx.auth_method == "api_key"


def test_key_cache_avoids_refetching_within_ttl(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _patch_secrets(monkeypatch, {"arn:x": "value"})

    first = auth_module._get_secret_value("arn:x")
    second = auth_module._get_secret_value("arn:x")

    assert first == second == "value"
    assert fake.calls == ["arn:x"]


def test_key_cache_refetches_after_ttl_expires(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _patch_secrets(monkeypatch, {"arn:x": "value"})
    monkeypatch.setattr(auth_module, "_CACHE_TTL_SECONDS", 0)

    auth_module._get_secret_value("arn:x")
    auth_module._get_secret_value("arn:x")

    assert fake.calls == ["arn:x", "arn:x"]


def test_key_cache_is_not_an_lru_cache() -> None:
    """CLAUDE.md Phase 1 'What not to do' — an @lru_cache never expires, so
    a rotated/revoked key would keep validating forever. Guard against a
    future refactor silently reintroducing one. A decorator usage requires
    `functools` to be imported somewhere in the module; checking for that
    import (rather than the bare word "lru_cache") avoids a false positive
    from this module's own comment explaining why it was rejected."""
    import inspect

    source = inspect.getsource(auth_module)
    assert "functools" not in source
