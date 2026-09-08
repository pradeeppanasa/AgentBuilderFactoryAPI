"""Unit tests for the Sprint 3 Phase 1 auth layer (CLAUDE.md Section 64.1).

Exercises ApiKeyAuthProvider/JwtAuthProvider/AuthChain directly, decoupled
from FastAPI — the HTTP-level 401/403 behaviour (auth_middleware) is
covered separately in tests/test_main.py.
"""

from __future__ import annotations

import time
from typing import Any

import auth as auth_module
import pytest
from auth import ApiKeyAuthProvider, AuthChain, JwtAuthProvider


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


async def test_jwt_provider_is_a_noop_stub() -> None:
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
