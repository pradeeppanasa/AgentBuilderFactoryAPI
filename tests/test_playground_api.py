"""API tests for POST /api/v1/agents/{agent_id}/playground (CLAUDE.md
Section 37.9/37.11).

litellm.acompletion is monkeypatched (matches test_model_router.py's
convention — no real model call). The guardrail engine's Bedrock client is
swapped for `FakeBedrockGuardrailClient` via dependency override (matches
test_connectors_api.py's `mock_connector_tester` convention) since moto
doesn't model bedrock-runtime's ApplyGuardrail API.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import litellm
import pytest
from fastapi.testclient import TestClient

from app.dependencies import get_guardrail_engine
from app.main import app
from app.modules.guardrails.engine import GuardrailEngine
from tests.fakes import FakeBedrockGuardrailClient, FakeToxicityClassifier

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
    engine = GuardrailEngine(bedrock, classifier_factory=lambda _m: FakeToxicityClassifier(0.5))
    app.dependency_overrides[get_guardrail_engine] = lambda: engine
    try:
        yield engine, bedrock
    finally:
        del app.dependency_overrides[get_guardrail_engine]


async def _create_agent(client: TestClient, token: str, **config_overrides: Any) -> str:
    response = client.post(
        "/api/v1/agents",
        json={
            "name": "Playground Agent",
            "description": "d",
            "business_purpose": "p",
            "agent_type": "conversational",
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
    make_user_and_token, guardrail_engine_with_fakes
) -> None:
    _, admin_token = await make_user_and_token(TENANT_A, role="admin")
    engine, bedrock = guardrail_engine_with_fakes
    bedrock.intervene = True  # any bedrock call blocks

    with TestClient(app) as client:
        policy = client.post(
            "/api/v1/platform/guardrail-policies",
            json={"name": "Blocking", "description": "d", "bedrock_guardrail_id": "gr-1"},
            headers=_bearer(admin_token),
        )
        policy_id = policy.json()["policy_id"]

        agent_id = await _create_agent(client, admin_token, guardrail_policy_id=policy_id)

        response = client.post(
            f"/api/v1/agents/{agent_id}/playground",
            json={"message": "ambiguous message"},
            headers=_bearer(admin_token),
        )

    assert response.status_code == 200
    body = response.json()
    assert body["blocked"] is True
    assert body["message"] == "This message was blocked by a guardrail policy."
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
