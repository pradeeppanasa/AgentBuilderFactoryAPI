"""API tests for POST /api/v1/agents/{agent_id}/playground (CLAUDE.md
Section 37.9/37.11).

litellm.acompletion is monkeypatched (matches test_model_router.py's
convention — no real model call). The guardrail engine's Bedrock client is
swapped for `FakeBedrockGuardrailClient` via dependency override (matches
test_connectors_api.py's `mock_connector_tester` convention) since moto
doesn't model bedrock-runtime's ApplyGuardrail API.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any

import litellm
import pytest
from fastapi.testclient import TestClient

from app.dependencies import get_bedrock_guardrail_provisioner, get_guardrail_engine
from app.main import app
from app.modules.guardrails.engine import GuardrailEngine
from app.modules.guardrails.provisioner import BedrockGuardrailProvisioner
from tests.fakes import (
    FakeBedrockControlPlaneClient,
    FakeBedrockGuardrailClient,
    FakeToxicityClassifier,
)

TENANT_A = "tenant-a"


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


class _FakeMessage:
    def __init__(self, content: str) -> None:
        self.content = content


class _FakeChoice:
    def __init__(self, content: str) -> None:
        self.message = _FakeMessage(content)


class _FakeUsage:
    def __init__(self, prompt_tokens: int, completion_tokens: int) -> None:
        self.prompt_tokens = prompt_tokens
        self.completion_tokens = completion_tokens
        self.total_tokens = prompt_tokens + completion_tokens


class _FakeResponse:
    def __init__(self, content: str) -> None:
        self.choices = [_FakeChoice(content)]
        self.usage = _FakeUsage(prompt_tokens=10, completion_tokens=20)


@pytest.fixture(autouse=True)
def _fake_llm(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _fake_acompletion(**kwargs: Any) -> _FakeResponse:
        return _FakeResponse("Hello from the playground.")

    monkeypatch.setattr(litellm, "acompletion", _fake_acompletion)
    monkeypatch.setattr(litellm, "completion_cost", lambda completion_response: 0.0013)


@pytest.fixture
def guardrail_engine_with_fakes() -> Iterator[tuple[GuardrailEngine, FakeBedrockGuardrailClient]]:
    bedrock = FakeBedrockGuardrailClient()
    # 0.5 lands in the toxicity check's default escalate band (0.40-0.85),
    # which is what this fixture's one caller wants — but nsfw/injection/
    # gibberish are exercised by test_guardrail_engine.py separately, not
    # here, so the policy this fixture is used with must disable them
    # explicitly (see test_playground_blocks_input_via_guardrail_policy)
    # or a uniform 0.5 score would also trip prompt_injection's much lower
    # default threshold (0.30) before ever reaching Bedrock.
    engine = GuardrailEngine(bedrock, classifier_factory=lambda _m, _k: FakeToxicityClassifier(0.5))
    app.dependency_overrides[get_guardrail_engine] = lambda: engine
    try:
        yield engine, bedrock
    finally:
        del app.dependency_overrides[get_guardrail_engine]


@pytest.fixture
def fake_bedrock_provisioner() -> Iterator[None]:
    """Overrides the Bedrock auto-provisioning dependency so creating a
    guardrail policy in these tests never makes a real (moto-unsupported)
    create_guardrail call — see test_guardrail_policies_api.py's identical
    fixture."""
    client = FakeBedrockControlPlaneClient()
    app.dependency_overrides[get_bedrock_guardrail_provisioner] = lambda: (
        BedrockGuardrailProvisioner(client)
    )
    try:
        yield
    finally:
        del app.dependency_overrides[get_bedrock_guardrail_provisioner]


async def _create_agent(client: TestClient, token: str, **config_overrides: Any) -> str:
    response = client.post(
        "/api/v1/agents",
        json={
            "name": "Playground Agent",
            "description": "d",
            "business_purpose": "p",
            "agent_type": "standard",
            "configuration": {
                "model_id": "anthropic.claude-3-5-sonnet-20241022-v2:0",
                "model_provider": "bedrock",
                "system_prompt": "You are a helpful agent.",
                **config_overrides,
            },
        },
        headers=_bearer(token),
    )
    assert response.status_code == 201
    return str(response.json()["agent_id"])


async def test_playground_turn_without_guardrail_policy(make_user_and_token) -> None:
    _, token = await make_user_and_token(TENANT_A, role="developer")

    with TestClient(app) as client:
        agent_id = await _create_agent(client, token)

        response = client.post(
            f"/api/v1/agents/{agent_id}/playground",
            json={"message": "hi there"},
            headers=_bearer(token),
        )

    assert response.status_code == 200
    body = response.json()
    assert body["blocked"] is False
    assert body["message"] == "Hello from the playground."
    assert body["metrics"]["tokens"]["input_tokens"] == 10
    assert body["metrics"]["tokens"]["output_tokens"] == 20
    assert body["metrics"]["estimated_cost_usd"] == 0.0013
    assert body["metrics"]["guardrail_decisions"] == []
    assert body["metrics"]["tool_calls"] == []
    assert body["metrics"]["kb_retrievals"] is None
    assert body["metrics"]["memory"]["session_entries"] == 2


async def test_playground_model_call_failure_returns_502_with_detail(
    make_user_and_token, monkeypatch: pytest.MonkeyPatch
) -> None:
    """call_model has no error handling of its own — an LLM-side failure
    (e.g. Bedrock auth rejecting local dev's placeholder credentials) must
    never surface as a bare, detail-less 500."""
    _, token = await make_user_and_token(TENANT_A, role="developer")

    with TestClient(app) as client:
        agent_id = await _create_agent(client, token)

        async def _fake_acompletion_raises(**kwargs: Any) -> None:
            raise litellm.exceptions.AuthenticationError(
                message="Invalid Authentication",
                llm_provider="bedrock",
                model="anthropic.claude-3-5-sonnet-20241022-v2:0",
            )

        monkeypatch.setattr(litellm, "acompletion", _fake_acompletion_raises)

        response = client.post(
            f"/api/v1/agents/{agent_id}/playground",
            json={"message": "hi there"},
            headers=_bearer(token),
        )

    assert response.status_code == 502
    detail = response.json()["detail"]
    assert detail["error"] == "llm_auth_failed"
    assert "credentials are invalid" in detail["message"]
    assert detail["provider"] == "bedrock"
    assert detail["model_id"] == "anthropic.claude-3-5-sonnet-20241022-v2:0"


async def test_playground_mock_mode_never_calls_the_model(
    make_user_and_token, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Wizard Redesign QA A-02/U-10 — ?mock=true must short-circuit before
    ever touching litellm, so the playground is exercisable without real
    Bedrock credentials (e.g. against local dev's placeholder AWS creds)."""
    _, token = await make_user_and_token(TENANT_A, role="developer")

    with TestClient(app) as client:
        agent_id = await _create_agent(client, token)

        async def _fail_if_called(**kwargs: Any) -> None:
            raise AssertionError("mock mode must never call litellm.acompletion")

        monkeypatch.setattr(litellm, "acompletion", _fail_if_called)

        response = client.post(
            f"/api/v1/agents/{agent_id}/playground?mock=true",
            json={"message": "hi there"},
            headers=_bearer(token),
        )

    assert response.status_code == 200
    body = response.json()
    assert body["blocked"] is False
    assert body["message"] == "Mock response — LLM not called."
    assert body["metrics"]["tokens"]["input_tokens"] == 120
    assert body["metrics"]["tokens"]["output_tokens"] == 64
    assert body["metrics"]["estimated_cost_usd"] == 0.0
    assert body["metrics"]["guardrail_decisions"] == []


async def test_playground_mock_mode_shapes_reply_from_agents_own_output_schema(
    make_user_and_token, monkeypatch: pytest.MonkeyPatch
) -> None:
    """CLAUDE.md Section 39.7 (A-02 clarified): the mock reply must be shaped
    from *this agent's own* configured output_schema, not one fixed generic
    string regardless of agent type — and never hardcoded to any one
    domain's fields (e.g. KYC-specific keys) since /playground is shared
    across every agent."""
    _, token = await make_user_and_token(TENANT_A, role="developer")

    async def _fail_if_called(**kwargs: Any) -> None:
        raise AssertionError("mock mode must never call litellm.acompletion")

    monkeypatch.setattr(litellm, "acompletion", _fail_if_called)

    with TestClient(app) as client:
        agent_id = await _create_agent(
            client,
            token,
            output_schema={
                "format": "json",
                "schema_definition": {
                    "type": "object",
                    "properties": {
                        "status": {"type": "string", "enum": ["VERIFIED", "UNVERIFIED"]},
                        "confidence": {"type": "number"},
                        "flags": {"type": "array", "items": {"type": "string"}},
                    },
                },
            },
        )

        response = client.post(
            f"/api/v1/agents/{agent_id}/playground?mock=true",
            json={"message": "hi there"},
            headers=_bearer(token),
        )

    assert response.status_code == 200
    body = response.json()
    parsed = json.loads(body["message"])
    assert parsed == {"status": "VERIFIED", "confidence": 0.0, "flags": []}


async def test_playground_mock_mode_never_calls_the_llm(
    make_user_and_token, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Wizard Redesign QA A-02/U-10 — ?mock=true must short-circuit before
    any Bedrock call, so it works with local dev's placeholder credentials."""
    _, token = await make_user_and_token(TENANT_A, role="developer")

    async def _fail_if_called(**kwargs: Any) -> None:
        raise AssertionError("litellm.acompletion must not be called in mock mode")

    monkeypatch.setattr(litellm, "acompletion", _fail_if_called)

    with TestClient(app) as client:
        agent_id = await _create_agent(client, token)

        response = client.post(
            f"/api/v1/agents/{agent_id}/playground?mock=true",
            json={"message": "hi there"},
            headers=_bearer(token),
        )

    assert response.status_code == 200
    body = response.json()
    assert body["blocked"] is False
    assert body["message"] == "Mock response — LLM not called."
    assert body["metrics"]["tokens"]["input_tokens"] == 120
    assert body["metrics"]["tokens"]["output_tokens"] == 64
    assert body["metrics"]["estimated_cost_usd"] == 0.0
    assert body["metrics"]["guardrail_decisions"] == []


async def test_playground_session_round_trips_turns(make_user_and_token) -> None:
    _, token = await make_user_and_token(TENANT_A, role="developer")

    with TestClient(app) as client:
        agent_id = await _create_agent(client, token)

        first = client.post(
            f"/api/v1/agents/{agent_id}/playground",
            json={"message": "first message"},
            headers=_bearer(token),
        )
        session_id = first.json()["session_id"]

        second = client.post(
            f"/api/v1/agents/{agent_id}/playground",
            json={"message": "second message", "session_id": session_id},
            headers=_bearer(token),
        )

    assert second.json()["session_id"] == session_id
    assert second.json()["metrics"]["memory"]["session_entries"] == 4


async def test_playground_blocks_input_via_guardrail_policy(
    make_user_and_token, guardrail_engine_with_fakes, fake_bedrock_provisioner: None
) -> None:
    _, admin_token = await make_user_and_token(TENANT_A, role="admin")
    engine, bedrock = guardrail_engine_with_fakes
    bedrock.intervene = True  # any bedrock call blocks

    with TestClient(app) as client:
        policy = client.post(
            "/api/v1/platform/guardrail-policies",
            json={
                "name": "Blocking",
                "description": "d",
                # Only exercise toxicity's escalate-to-Bedrock path here —
                # the fixed 0.5 fake score would also trip prompt_injection's
                # much lower default threshold (0.30) otherwise.
                "bert": {
                    "check_nsfw": False,
                    "check_prompt_injection": False,
                    "check_gibberish": False,
                },
            },
            headers=_bearer(admin_token),
        )
        policy_id = policy.json()["policy_id"]
        assert policy.json()["bedrock_guardrail_id"] is not None  # auto-provisioned

        agent_id = await _create_agent(client, admin_token, guardrail_policy_id=policy_id)

        response = client.post(
            f"/api/v1/agents/{agent_id}/playground",
            json={"message": "ambiguous message"},
            headers=_bearer(admin_token),
        )

    assert response.status_code == 200
    body = response.json()
    assert body["blocked"] is True
    # Now sourced from the policy's own configured message (defaults shown
    # here), not a hardcoded playground-only string — see playground.py.
    assert body["message"] == "This content has been blocked by the content policy."
    assert any(d["action"] == "block" for d in body["metrics"]["guardrail_decisions"])


async def test_playground_overrides_forbidden_for_non_admin(make_user_and_token) -> None:
    _, dev_token = await make_user_and_token(TENANT_A, role="developer")

    with TestClient(app) as client:
        agent_id = await _create_agent(client, dev_token)

        response = client.post(
            f"/api/v1/agents/{agent_id}/playground",
            json={"message": "hi", "overrides": {"disable_guardrails": True}},
            headers=_bearer(dev_token),
        )

    assert response.status_code == 403


async def test_playground_temperature_override_allowed_for_admin(make_user_and_token) -> None:
    _, admin_token = await make_user_and_token(TENANT_A, role="admin")

    with TestClient(app) as client:
        agent_id = await _create_agent(client, admin_token)

        response = client.post(
            f"/api/v1/agents/{agent_id}/playground",
            json={"message": "hi", "overrides": {"temperature": 0.9}},
            headers=_bearer(admin_token),
        )

    assert response.status_code == 200


async def test_playground_404_for_unknown_agent(make_user_and_token) -> None:
    _, token = await make_user_and_token(TENANT_A, role="developer")

    with TestClient(app) as client:
        response = client.post(
            "/api/v1/agents/does-not-exist/playground",
            json={"message": "hi"},
            headers=_bearer(token),
        )

    assert response.status_code == 404
