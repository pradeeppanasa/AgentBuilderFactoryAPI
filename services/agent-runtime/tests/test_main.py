"""main.py loads its config and builds AgentOrchestrator at import time
(module-level, by design — "load config once on startup", same as the
instruction's own skeleton) — so config_loader.load_agent_config must be
patched BEFORE main is first imported in this process. sub-components
(LLMClient, RAGClient, …) are safe to construct for real here: building a
boto3 client object never makes a network call or needs real credentials,
only actually *calling* one does — and these tests never call /chat
without first replacing orchestrator.run with a fake.

Sprint 3 Phase 1 (CLAUDE.md Section 64.1) replaced the old AGENT_API_KEY
env-var check with auth_middleware, backed by auth.auth_chain (Secrets
Manager) and config_loader.get_current_agent_record (a fresh per-request
DynamoDB read, separate from the startup-cached agent_config). Every test
here also patches get_current_agent_record and auth's Secrets Manager
client, and neuters audit writes to a fast in-memory recorder — none of
these tests should ever attempt a real AWS call.
"""

from __future__ import annotations

import json as _json
import sys
import time
from typing import Any

import auth as auth_module
import jwt as _pyjwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa as _rsa
from fastapi.testclient import TestClient


class _FakeSecretsClient:
    def __init__(self, values: dict[str, str]) -> None:
        self._values = values

    def get_secret_value(self, SecretId: str) -> dict[str, str]:
        return {"SecretString": self._values[SecretId]}


class _AuditRecorder:
    """Replaces audit.write_audit_event in-process — fast, no AWS call,
    and lets tests assert on exactly what was recorded."""

    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    def __call__(self, **kwargs: Any) -> None:
        self.events.append(kwargs)


@pytest.fixture(autouse=True)
def _reset_auth_key_cache() -> None:
    auth_module._key_cache.clear()
    yield
    auth_module._key_cache.clear()


def _fresh_main(
    monkeypatch: pytest.MonkeyPatch,
    config: dict[str, Any],
    agent_record: dict[str, Any] | None = None,
    secrets: dict[str, str] | None = None,
) -> Any:
    monkeypatch.setenv("AGENT_ID", config["agent_id"])
    monkeypatch.setenv("TENANT_ID", config["tenant_id"])

    import config_loader

    monkeypatch.setattr(config_loader, "load_agent_config", lambda dynamodb=None: config)
    monkeypatch.setattr(
        config_loader,
        "get_current_agent_record",
        lambda dynamodb=None: agent_record
        if agent_record is not None
        else {"tenant_id": config["tenant_id"], "agent_id": config["agent_id"]},
    )

    monkeypatch.setattr(
        auth_module, "_get_secrets_client", lambda: _FakeSecretsClient(secrets or {})
    )

    sys.modules.pop("main", None)
    import main as main_module

    monkeypatch.setattr(main_module, "write_audit_event", _AuditRecorder())

    return main_module


def _config(**overrides: Any) -> dict[str, Any]:
    defaults: dict[str, Any] = {
        "agent_id": "faq-agent-1",
        "tenant_id": "tenant-a",
        "name": "FAQ Agent",
        "version": 3,
        "model_id": "anthropic.claude-3-5-sonnet-20241022-v2:0",
        "model_provider": "bedrock",
        "system_prompt": "You are a FAQ agent.",
    }
    defaults.update(overrides)
    return defaults


_KEY_ARN = "arn:aws:secretsmanager:eu-west-2:123456789012:secret:faq-agent-1-api-key"


def _agent_record_with_key(**overrides: Any) -> dict[str, Any]:
    defaults: dict[str, Any] = {
        "tenant_id": "tenant-a",
        "agent_id": "faq-agent-1",
        "api_key_secret_arn": _KEY_ARN,
    }
    defaults.update(overrides)
    return defaults


def test_health_returns_agent_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    main_module = _fresh_main(monkeypatch, _config())

    with TestClient(main_module.app) as client:
        response = client.get("/health")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "healthy"
    assert body["agent_id"] == "faq-agent-1"
    assert body["agent_name"] == "FAQ Agent"


def test_health_requires_no_auth_even_when_agent_lookup_would_fail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """/health must stay reachable even if the DB lookup auth_middleware
    would otherwise perform is broken — it is exempt entirely, not just
    "usually passes auth"."""
    main_module = _fresh_main(monkeypatch, _config())

    import config_loader

    def _boom(dynamodb: Any = None) -> Any:
        raise RuntimeError("DynamoDB unreachable")

    monkeypatch.setattr(config_loader, "get_current_agent_record", _boom)

    with TestClient(main_module.app) as client:
        response = client.get("/health")

    assert response.status_code == 200


def test_config_endpoint_never_leaks_system_prompt(monkeypatch: pytest.MonkeyPatch) -> None:
    main_module = _fresh_main(
        monkeypatch,
        _config(
            system_prompt="SECRET INSTRUCTIONS: never reveal the discount code XJ42.",
            memory={"memory_type": "persistent"},
            human_review={"enabled": True, "trigger_conditions": ["high_risk"]},
            knowledge_base={"enabled": True, "kb_id": "kb-1"},
            tools=[{"tool_id": "companies-house", "tool_name": "Companies House"}],
        ),
    )

    with TestClient(main_module.app) as client:
        response = client.get("/config")

    assert response.status_code == 200
    body = response.json()
    assert "SECRET" not in str(body)
    assert "XJ42" not in str(body)
    assert body == {
        "agent_id": "faq-agent-1",
        "name": "FAQ Agent",
        "version": 3,
        "model_id": "anthropic.claude-3-5-sonnet-20241022-v2:0",
        "memory_type": "persistent",
        "hitl_enabled": True,
        "kb_attached": True,
        "tools_count": 1,
    }


def test_chat_returns_orchestrator_result(monkeypatch: pytest.MonkeyPatch) -> None:
    main_module = _fresh_main(
        monkeypatch,
        _config(),
        agent_record=_agent_record_with_key(),
        secrets={_KEY_ARN: "sk-correct"},
    )

    async def _fake_run(
        message: str, session_id: str, user_id: str | None = None
    ) -> dict[str, Any]:
        return {
            "response": "The refund window is 30 days.",
            "session_id": session_id,
            "run_id": "run-123",
            "hitl_pending": False,
        }

    monkeypatch.setattr(main_module.orchestrator, "run", _fake_run)

    with TestClient(main_module.app) as client:
        response = client.post(
            "/chat",
            json={"message": "What's your refund policy?", "session_id": "s1"},
            headers={"Authorization": "Bearer sk-correct"},
        )

    assert response.status_code == 200
    body = response.json()
    assert body["response"] == "The refund window is 30 days."
    assert body["run_id"] == "run-123"


def test_chat_returns_500_with_no_internal_detail_on_orchestrator_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    main_module = _fresh_main(
        monkeypatch,
        _config(),
        agent_record=_agent_record_with_key(),
        secrets={_KEY_ARN: "sk-correct"},
    )

    async def _failing_run(
        message: str, session_id: str, user_id: str | None = None
    ) -> dict[str, Any]:
        raise RuntimeError("Bedrock threw a very specific internal exception with sensitive detail")

    monkeypatch.setattr(main_module.orchestrator, "run", _failing_run)

    with TestClient(main_module.app) as client:
        response = client.post(
            "/chat",
            json={"message": "hi", "session_id": "s1"},
            headers={"Authorization": "Bearer sk-correct"},
        )

    assert response.status_code == 500
    assert "sensitive detail" not in response.text
    assert response.json()["detail"] == "Agent execution failed"


def test_chat_with_valid_api_key_returns_200(monkeypatch: pytest.MonkeyPatch) -> None:
    main_module = _fresh_main(
        monkeypatch,
        _config(),
        agent_record=_agent_record_with_key(),
        secrets={_KEY_ARN: "sk-correct"},
    )

    async def _fake_run(
        message: str, session_id: str, user_id: str | None = None
    ) -> dict[str, Any]:
        return {"response": "ok", "session_id": session_id, "run_id": "r1", "hitl_pending": False}

    monkeypatch.setattr(main_module.orchestrator, "run", _fake_run)

    with TestClient(main_module.app) as client:
        response = client.post(
            "/chat",
            json={"message": "hi", "session_id": "s1"},
            headers={"Authorization": "Bearer sk-correct"},
        )

    assert response.status_code == 200


def test_chat_with_wrong_api_key_returns_401(monkeypatch: pytest.MonkeyPatch) -> None:
    main_module = _fresh_main(
        monkeypatch,
        _config(),
        agent_record=_agent_record_with_key(),
        secrets={_KEY_ARN: "sk-correct"},
    )

    with TestClient(main_module.app) as client:
        response = client.post(
            "/chat",
            json={"message": "hi", "session_id": "s1"},
            headers={"Authorization": "Bearer wrong-key"},
        )

    assert response.status_code == 401
    audit_events = main_module.write_audit_event.events
    assert any(e["event_type"] == "auth.failed" for e in audit_events)


def test_chat_with_no_authorization_header_returns_401(monkeypatch: pytest.MonkeyPatch) -> None:
    main_module = _fresh_main(
        monkeypatch,
        _config(),
        agent_record=_agent_record_with_key(),
        secrets={_KEY_ARN: "sk-correct"},
    )

    with TestClient(main_module.app) as client:
        response = client.post("/chat", json={"message": "hi", "session_id": "s1"})

    assert response.status_code == 401


def test_chat_with_no_key_provisioned_for_agent_returns_401(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An agent with no api_key_secret_arn at all must default-deny —
    never silently allow every caller through."""
    main_module = _fresh_main(
        monkeypatch,
        _config(),
        agent_record={"tenant_id": "tenant-a", "agent_id": "faq-agent-1"},
    )

    with TestClient(main_module.app) as client:
        response = client.post(
            "/chat",
            json={"message": "hi", "session_id": "s1"},
            headers={"Authorization": "Bearer anything"},
        )

    assert response.status_code == 401


def test_chat_with_revoked_key_returns_401_even_if_key_value_matches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    main_module = _fresh_main(
        monkeypatch,
        _config(),
        agent_record=_agent_record_with_key(api_key_revoked=True),
        secrets={_KEY_ARN: "sk-correct"},
    )

    with TestClient(main_module.app) as client:
        response = client.post(
            "/chat",
            json={"message": "hi", "session_id": "s1"},
            headers={"Authorization": "Bearer sk-correct"},
        )

    assert response.status_code == 401


def test_chat_accepts_previous_key_during_rotation_grace_period(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prev_arn = "arn:aws:secretsmanager:eu-west-2:123456789012:secret:faq-agent-1-api-key-old"
    main_module = _fresh_main(
        monkeypatch,
        _config(),
        agent_record=_agent_record_with_key(
            previous_api_key_secret_arn=prev_arn,
            previous_key_expires_at=time.time() + 3600,
        ),
        secrets={_KEY_ARN: "sk-new", prev_arn: "sk-old"},
    )

    async def _fake_run(
        message: str, session_id: str, user_id: str | None = None
    ) -> dict[str, Any]:
        return {"response": "ok", "session_id": session_id, "run_id": "r1", "hitl_pending": False}

    monkeypatch.setattr(main_module.orchestrator, "run", _fake_run)

    with TestClient(main_module.app) as client:
        response = client.post(
            "/chat",
            json={"message": "hi", "session_id": "s1"},
            headers={"Authorization": "Bearer sk-old"},
        )

    assert response.status_code == 200


def test_chat_rejects_previous_key_once_grace_period_has_expired(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prev_arn = "arn:aws:secretsmanager:eu-west-2:123456789012:secret:faq-agent-1-api-key-old"
    main_module = _fresh_main(
        monkeypatch,
        _config(),
        agent_record=_agent_record_with_key(
            previous_api_key_secret_arn=prev_arn,
            previous_key_expires_at=time.time() - 10,
        ),
        secrets={_KEY_ARN: "sk-new", prev_arn: "sk-old"},
    )

    with TestClient(main_module.app) as client:
        response = client.post(
            "/chat",
            json={"message": "hi", "session_id": "s1"},
            headers={"Authorization": "Bearer sk-old"},
        )

    assert response.status_code == 401


def test_chat_tenant_mismatch_returns_403_and_writes_audit_event(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """R67 — even a caller who authenticates successfully must be denied if
    the resulting AuthContext's tenant doesn't match this agent's own DB
    record. Not naturally reachable through ApiKeyAuthProvider alone today
    (its AuthContext.tenant_id is always copied from the very record being
    checked) — this exercises the middleware's own comparison directly by
    swapping in a fake auth chain, decoupled from which real provider
    would produce a mismatched tenant claim (see
    test_chat_with_jwt_tenant_mismatch_returns_403 below for that, via the
    real JwtAuthProvider, S-12)."""
    main_module = _fresh_main(
        monkeypatch,
        _config(),
        agent_record=_agent_record_with_key(),
        secrets={_KEY_ARN: "sk-correct"},
    )

    from auth import AuthContext

    class _FakeChain:
        async def authenticate(
            self, authorization: str, agent_record: dict[str, Any]
        ) -> AuthContext:
            return AuthContext(
                tenant_id="some-other-tenant",
                agent_id=agent_record["agent_id"],
                principal_id="jwt-caller",
                auth_method="jwt",
            )

    monkeypatch.setattr(main_module, "auth_chain", _FakeChain())

    with TestClient(main_module.app) as client:
        response = client.post(
            "/chat",
            json={"message": "hi", "session_id": "s1"},
            headers={"Authorization": "Bearer sk-correct"},
        )

    assert response.status_code == 403
    audit_events = main_module.write_audit_event.events
    mismatch_events = [e for e in audit_events if e["event_type"] == "auth.tenant_mismatch"]
    assert len(mismatch_events) == 1
    assert mismatch_events[0]["tenant_id"] == "some-other-tenant"
    assert mismatch_events[0]["result"] == "denied"


# ── Sprint 4 Phase 4 (S-12) — end-to-end through the REAL auth_chain and
# JwtAuthProvider, not a fake chain (test_auth.py covers the provider's
# own logic in isolation; these confirm main.py actually wires it up).

_JWT_PRIVATE_KEY = _rsa.generate_private_key(public_exponent=65537, key_size=2048)
_JWT_KID = "test-key-1"
_JWT_ISSUER = "https://idp.example.com/"
_JWT_JWKS_URL = "https://idp.example.com/.well-known/jwks.json"


def _jwt_jwks_document() -> dict[str, Any]:
    jwk = _json.loads(_pyjwt.algorithms.RSAAlgorithm.to_jwk(_JWT_PRIVATE_KEY.public_key()))
    jwk["kid"] = _JWT_KID
    return {"keys": [jwk]}


def _make_jwt(tenant_id: str) -> str:
    now = int(time.time())
    payload = {"iss": _JWT_ISSUER, "tenant_id": tenant_id, "iat": now, "exp": now + 300}
    return _pyjwt.encode(payload, _JWT_PRIVATE_KEY, algorithm="RS256", headers={"kid": _JWT_KID})


def test_chat_with_valid_jwt_succeeds(monkeypatch: pytest.MonkeyPatch) -> None:
    """ApiKeyAuthProvider defers (no api_key_secret_arn on this record),
    so this exercises AuthChain falling through to the real JwtAuthProvider."""
    main_module = _fresh_main(
        monkeypatch,
        _config(),
        agent_record=_agent_record_with_key(
            api_key_secret_arn=None, jwt_issuer=_JWT_ISSUER, jwt_jwks_url=_JWT_JWKS_URL
        ),
    )
    monkeypatch.setattr(auth_module, "_fetch_jwks", lambda url: _jwt_jwks_document())
    _fake_run_ok(monkeypatch, main_module)

    with TestClient(main_module.app) as client:
        response = client.post(
            "/chat",
            json={"message": "hi", "session_id": "s1"},
            headers={"Authorization": f"Bearer {_make_jwt('tenant-a')}"},
        )

    assert response.status_code == 200


def test_chat_with_jwt_tenant_mismatch_returns_403(monkeypatch: pytest.MonkeyPatch) -> None:
    main_module = _fresh_main(
        monkeypatch,
        _config(),
        agent_record=_agent_record_with_key(
            api_key_secret_arn=None, jwt_issuer=_JWT_ISSUER, jwt_jwks_url=_JWT_JWKS_URL
        ),
    )
    monkeypatch.setattr(auth_module, "_fetch_jwks", lambda url: _jwt_jwks_document())
    _fake_run_ok(monkeypatch, main_module)

    with TestClient(main_module.app) as client:
        response = client.post(
            "/chat",
            json={"message": "hi", "session_id": "s1"},
            headers={"Authorization": f"Bearer {_make_jwt('some-other-tenant')}"},
        )

    assert response.status_code == 403
    audit_events = main_module.write_audit_event.events
    mismatch_events = [e for e in audit_events if e["event_type"] == "auth.tenant_mismatch"]
    assert len(mismatch_events) == 1
    assert mismatch_events[0]["tenant_id"] == "some-other-tenant"


def test_chat_with_jwt_when_agent_has_no_jwt_config_returns_401(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An agent with JWT auth off entirely (no jwt_issuer/jwt_jwks_url) —
    both providers defer, AuthChain returns None, 401."""
    main_module = _fresh_main(
        monkeypatch,
        _config(),
        agent_record=_agent_record_with_key(api_key_secret_arn=None),
    )

    with TestClient(main_module.app) as client:
        response = client.post(
            "/chat",
            json={"message": "hi", "session_id": "s1"},
            headers={"Authorization": f"Bearer {_make_jwt('tenant-a')}"},
        )

    assert response.status_code == 401


def test_chat_returns_403_when_orchestrator_raises_tool_denied_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Sprint 3 Phase 4 (R64) — HTTP-layer mapping only; ToolPolicyEngine's
    own decision logic is covered directly in tests/test_tool_policy.py,
    and its propagation out of ToolExecutor.execute() in
    tests/test_tool_executor.py."""
    main_module = _fresh_main(
        monkeypatch,
        _config(),
        agent_record=_agent_record_with_key(),
        secrets={_KEY_ARN: "sk-correct"},
    )

    from tool_policy import ToolDeniedError

    async def _denied_run(message: str, session_id: str, user_id: str | None = None) -> Any:
        raise ToolDeniedError(
            "db-delete", "Tool 'db-delete' not in agent tool policy — default deny"
        )

    monkeypatch.setattr(main_module.orchestrator, "run", _denied_run)

    with TestClient(main_module.app) as client:
        response = client.post(
            "/chat",
            json={"message": "hi", "session_id": "s1"},
            headers={"Authorization": "Bearer sk-correct"},
        )

    assert response.status_code == 403
    assert "default deny" in response.json()["detail"]


def test_chat_returns_202_awaiting_approval_when_orchestrator_pauses_for_tool_approval(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Sprint 4 Phase 3 (S-11) — orchestrator.run() now catches
    ApprovalRequiredError itself (tests/test_orchestrator.py covers that
    directly) and returns a hitl_pending dict with an approval_review_id;
    this test only covers main.py's own plumbing of that dict into a 202."""
    main_module = _fresh_main(
        monkeypatch,
        _config(),
        agent_record=_agent_record_with_key(),
        secrets={_KEY_ARN: "sk-correct"},
    )

    async def _pending_run(message: str, session_id: str, user_id: str | None = None) -> Any:
        return {
            "response": "This action requires human approval before it can proceed.",
            "session_id": session_id,
            "run_id": "r1",
            "hitl_pending": True,
            "approval_review_id": "TAPR-ABCD1234",
            "approval_status": "pending",
            "latency_ms": 10,
            "input_tokens": 5,
            "output_tokens": 0,
        }

    monkeypatch.setattr(main_module.orchestrator, "run", _pending_run)

    with TestClient(main_module.app) as client:
        response = client.post(
            "/chat",
            json={"message": "hi", "session_id": "s1"},
            headers={"Authorization": "Bearer sk-correct"},
        )

    assert response.status_code == 202
    body = response.json()
    assert body["hitl_pending"] is True
    assert body["approval_review_id"] == "TAPR-ABCD1234"
    assert body["approval_status"] == "pending"


def test_resume_approval_returns_202_while_still_pending(monkeypatch: pytest.MonkeyPatch) -> None:
    main_module = _fresh_main(
        monkeypatch,
        _config(),
        agent_record=_agent_record_with_key(),
        secrets={_KEY_ARN: "sk-correct"},
    )

    async def _fake_resume(review_id: str) -> Any:
        return {
            "response": "This action is still awaiting human approval.",
            "session_id": "",
            "run_id": "r2",
            "hitl_pending": True,
            "approval_review_id": review_id,
            "approval_status": "pending",
            "latency_ms": 1,
            "input_tokens": 0,
            "output_tokens": 0,
        }

    monkeypatch.setattr(main_module.orchestrator, "resume_after_approval", _fake_resume)

    with TestClient(main_module.app) as client:
        response = client.post(
            "/approvals/TAPR-ABCD1234/resume", headers={"Authorization": "Bearer sk-correct"}
        )

    assert response.status_code == 202
    assert response.json()["approval_status"] == "pending"


def test_resume_approval_returns_200_once_approved_and_executed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    main_module = _fresh_main(
        monkeypatch,
        _config(),
        agent_record=_agent_record_with_key(),
        secrets={_KEY_ARN: "sk-correct"},
    )

    async def _fake_resume(review_id: str) -> Any:
        return {
            "response": "Payment transfer completed.",
            "session_id": "s1",
            "run_id": "r3",
            "hitl_pending": False,
            "approval_review_id": review_id,
            "approval_status": "approved",
            "latency_ms": 42,
            "input_tokens": 10,
            "output_tokens": 5,
        }

    monkeypatch.setattr(main_module.orchestrator, "resume_after_approval", _fake_resume)

    with TestClient(main_module.app) as client:
        response = client.post(
            "/approvals/TAPR-ABCD1234/resume", headers={"Authorization": "Bearer sk-correct"}
        )

    assert response.status_code == 200
    body = response.json()
    assert body["approval_status"] == "approved"
    assert body["response"] == "Payment transfer completed."


def test_resume_approval_requires_auth(monkeypatch: pytest.MonkeyPatch) -> None:
    main_module = _fresh_main(
        monkeypatch,
        _config(),
        agent_record=_agent_record_with_key(),
        secrets={_KEY_ARN: "sk-correct"},
    )

    with TestClient(main_module.app) as client:
        response = client.post("/approvals/TAPR-ABCD1234/resume")

    assert response.status_code == 401


def test_resume_approval_returns_500_with_no_internal_detail_on_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    main_module = _fresh_main(
        monkeypatch,
        _config(),
        agent_record=_agent_record_with_key(),
        secrets={_KEY_ARN: "sk-correct"},
    )

    async def _failing_resume(review_id: str) -> Any:
        raise RuntimeError("Bedrock threw a very specific internal exception with sensitive detail")

    monkeypatch.setattr(main_module.orchestrator, "resume_after_approval", _failing_resume)

    with TestClient(main_module.app) as client:
        response = client.post(
            "/approvals/TAPR-ABCD1234/resume", headers={"Authorization": "Bearer sk-correct"}
        )

    assert response.status_code == 500
    assert "sensitive detail" not in response.text
    assert response.json()["detail"] == "Agent execution failed"
    audit_events = main_module.write_audit_event.events
    error_events = [e for e in audit_events if e["result"] == "error"]
    assert len(error_events) == 1
    assert error_events[0]["action"] == "POST /approvals/TAPR-ABCD1234/resume"


# ── Sprint 4 Phase 2 (S-05, CLAUDE.md Section 61.2) ─────────────────────
# agent.invoked — success and error paths. ToolDeniedError/
# ApprovalRequiredError deliberately do NOT write agent.invoked (they have
# their own distinct HTTP mapping and aren't a completed/failed run in the
# Section 61.2 sense) — see test_main.py's earlier tests for those paths.


def test_chat_success_writes_agent_invoked_audit_event_with_telemetry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """latency_ms/input_tokens/output_tokens come from orchestrator.run()'s
    own return dict and must land in the audit event's extra — but never in
    the ChatResponse body itself, which only has the original 4 fields."""
    main_module = _fresh_main(
        monkeypatch,
        _config(),
        agent_record=_agent_record_with_key(),
        secrets={_KEY_ARN: "sk-correct"},
    )

    async def _fake_run(
        message: str, session_id: str, user_id: str | None = None
    ) -> dict[str, Any]:
        return {
            "response": "ok",
            "session_id": session_id,
            "run_id": "r1",
            "hitl_pending": False,
            "latency_ms": 42,
            "input_tokens": 10,
            "output_tokens": 5,
        }

    monkeypatch.setattr(main_module.orchestrator, "run", _fake_run)

    with TestClient(main_module.app) as client:
        response = client.post(
            "/chat",
            json={"message": "hi", "session_id": "s1"},
            headers={"Authorization": "Bearer sk-correct"},
        )

    assert response.status_code == 200
    assert "latency_ms" not in response.json()

    audit_events = main_module.write_audit_event.events
    invoked_events = [e for e in audit_events if e["event_type"] == "agent.invoked"]
    assert len(invoked_events) == 1
    assert invoked_events[0]["result"] == "success"
    assert invoked_events[0]["extra"] == {
        "latency_ms": 42,
        "input_tokens": 10,
        "output_tokens": 5,
    }


def test_chat_error_writes_agent_invoked_audit_event_with_error_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    main_module = _fresh_main(
        monkeypatch,
        _config(),
        agent_record=_agent_record_with_key(),
        secrets={_KEY_ARN: "sk-correct"},
    )

    async def _failing_run(
        message: str, session_id: str, user_id: str | None = None
    ) -> dict[str, Any]:
        raise RuntimeError("boom")

    monkeypatch.setattr(main_module.orchestrator, "run", _failing_run)

    with TestClient(main_module.app) as client:
        response = client.post(
            "/chat",
            json={"message": "hi", "session_id": "s1"},
            headers={"Authorization": "Bearer sk-correct"},
        )

    assert response.status_code == 500
    audit_events = main_module.write_audit_event.events
    invoked_events = [e for e in audit_events if e["event_type"] == "agent.invoked"]
    assert len(invoked_events) == 1
    assert invoked_events[0]["result"] == "error"
    assert invoked_events[0]["extra"] == {"error_type": "RuntimeError"}


# ── Sprint 3 Phase 9 (S-03, R70, CLAUDE.md Section 67) ──────────────────
# quota.py's own pure-logic unit tests live in test_quota.py; these cover
# only the auth_middleware wiring — that a 429/402 actually short-circuits
# the request before orchestrator.run() is ever called.


def _fake_run_ok(monkeypatch: pytest.MonkeyPatch, main_module: Any) -> None:
    async def _fake_run(message: str, session_id: str, user_id: str | None = None) -> Any:
        return {"response": "ok", "session_id": session_id, "run_id": "r1", "hitl_pending": False}

    monkeypatch.setattr(main_module.orchestrator, "run", _fake_run)


def test_chat_returns_429_once_rpm_limit_is_exceeded(monkeypatch: pytest.MonkeyPatch) -> None:
    import fakeredis

    main_module = _fresh_main(
        monkeypatch,
        _config(rate_limit_rpm=1),
        agent_record=_agent_record_with_key(),
        secrets={_KEY_ARN: "sk-correct"},
    )
    monkeypatch.setattr(main_module, "redis_client", fakeredis.FakeAsyncRedis())
    _fake_run_ok(monkeypatch, main_module)

    with TestClient(main_module.app) as client:
        first = client.post(
            "/chat",
            json={"message": "hi", "session_id": "s1"},
            headers={"Authorization": "Bearer sk-correct"},
        )
        second = client.post(
            "/chat",
            json={"message": "hi again", "session_id": "s1"},
            headers={"Authorization": "Bearer sk-correct"},
        )

    assert first.status_code == 200
    assert second.status_code == 429
    assert "rpm" in second.json()["detail"]


def test_chat_rate_limit_exceeded_writes_audit_event(monkeypatch: pytest.MonkeyPatch) -> None:
    """Sprint 4 Phase 2 (S-05, CLAUDE.md Section 61.2)."""
    import fakeredis

    main_module = _fresh_main(
        monkeypatch,
        _config(rate_limit_rpm=1),
        agent_record=_agent_record_with_key(),
        secrets={_KEY_ARN: "sk-correct"},
    )
    monkeypatch.setattr(main_module, "redis_client", fakeredis.FakeAsyncRedis())
    _fake_run_ok(monkeypatch, main_module)

    with TestClient(main_module.app) as client:
        client.post(
            "/chat",
            json={"message": "hi", "session_id": "s1"},
            headers={"Authorization": "Bearer sk-correct"},
        )
        second = client.post(
            "/chat",
            json={"message": "hi again", "session_id": "s1"},
            headers={"Authorization": "Bearer sk-correct"},
        )

    assert second.status_code == 429
    audit_events = main_module.write_audit_event.events
    exceeded_events = [e for e in audit_events if e["event_type"] == "rate_limit.exceeded"]
    assert len(exceeded_events) == 1
    assert exceeded_events[0]["result"] == "denied"
    assert exceeded_events[0]["extra"] == {"reason": "rpm_exceeded"}


def test_chat_rate_limit_fails_open_when_redis_unreachable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _ExplodingRedis:
        async def incr(self, key: str) -> int:
            raise ConnectionError("redis unavailable")

    main_module = _fresh_main(
        monkeypatch,
        _config(rate_limit_rpm=1),
        agent_record=_agent_record_with_key(),
        secrets={_KEY_ARN: "sk-correct"},
    )
    monkeypatch.setattr(main_module, "redis_client", _ExplodingRedis())
    _fake_run_ok(monkeypatch, main_module)

    with TestClient(main_module.app) as client:
        # Would be denied on the second call if Redis were reachable —
        # R29/R39: rate limiting fails open, so both still succeed.
        for _ in range(2):
            response = client.post(
                "/chat",
                json={"message": "hi", "session_id": "s1"},
                headers={"Authorization": "Bearer sk-correct"},
            )
            assert response.status_code == 200


def test_chat_returns_402_once_monthly_budget_is_exceeded(monkeypatch: pytest.MonkeyPatch) -> None:
    current_period = time.strftime("%Y-%m", time.gmtime())
    main_module = _fresh_main(
        monkeypatch,
        _config(monthly_budget_usd=1.0),
        agent_record=_agent_record_with_key(
            current_month_spend_usd=5.0, current_month_spend_period=current_period
        ),
        secrets={_KEY_ARN: "sk-correct"},
    )
    _fake_run_ok(monkeypatch, main_module)

    with TestClient(main_module.app) as client:
        response = client.post(
            "/chat",
            json={"message": "hi", "session_id": "s1"},
            headers={"Authorization": "Bearer sk-correct"},
        )

    assert response.status_code == 402


def test_chat_budget_exceeded_writes_audit_event(monkeypatch: pytest.MonkeyPatch) -> None:
    """Sprint 4 Phase 2 (S-05, CLAUDE.md Section 61.2)."""
    current_period = time.strftime("%Y-%m", time.gmtime())
    main_module = _fresh_main(
        monkeypatch,
        _config(monthly_budget_usd=1.0),
        agent_record=_agent_record_with_key(
            current_month_spend_usd=5.0, current_month_spend_period=current_period
        ),
        secrets={_KEY_ARN: "sk-correct"},
    )
    _fake_run_ok(monkeypatch, main_module)

    with TestClient(main_module.app) as client:
        response = client.post(
            "/chat",
            json={"message": "hi", "session_id": "s1"},
            headers={"Authorization": "Bearer sk-correct"},
        )

    assert response.status_code == 402
    audit_events = main_module.write_audit_event.events
    exceeded_events = [e for e in audit_events if e["event_type"] == "budget.exceeded"]
    assert len(exceeded_events) == 1
    assert exceeded_events[0]["result"] == "denied"


def test_chat_allowed_when_spend_is_within_monthly_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    current_period = time.strftime("%Y-%m", time.gmtime())
    main_module = _fresh_main(
        monkeypatch,
        _config(monthly_budget_usd=10.0),
        agent_record=_agent_record_with_key(
            current_month_spend_usd=1.0, current_month_spend_period=current_period
        ),
        secrets={_KEY_ARN: "sk-correct"},
    )
    _fake_run_ok(monkeypatch, main_module)

    with TestClient(main_module.app) as client:
        response = client.post(
            "/chat",
            json={"message": "hi", "session_id": "s1"},
            headers={"Authorization": "Bearer sk-correct"},
        )

    assert response.status_code == 200


def test_chat_without_quota_configured_never_touches_redis(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No rate_limit_rpm/rpd/monthly_budget_usd on the agent config at all
    — the quota check block must be skipped entirely, not run with
    effectively-infinite limits."""

    class _ExplodingRedis:
        async def incr(self, key: str) -> int:
            raise AssertionError("Redis should never be touched when no limit is configured")

    main_module = _fresh_main(
        monkeypatch,
        _config(),
        agent_record=_agent_record_with_key(),
        secrets={_KEY_ARN: "sk-correct"},
    )
    monkeypatch.setattr(main_module, "redis_client", _ExplodingRedis())
    _fake_run_ok(monkeypatch, main_module)

    with TestClient(main_module.app) as client:
        response = client.post(
            "/chat",
            json={"message": "hi", "session_id": "s1"},
            headers={"Authorization": "Bearer sk-correct"},
        )

    assert response.status_code == 200
