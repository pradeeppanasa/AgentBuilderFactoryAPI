"""API tests for POST /api/v1/agents/{generate,improve}-prompt (CLAUDE.md
Section 22). Same litellm.acompletion monkeypatch convention as
test_task_planner_api.py / test_model_router.py — no real model call.
"""

from __future__ import annotations

from typing import Any

import litellm
import pytest
from fastapi.testclient import TestClient

from app.main import app

TENANT_A = "tenant-a"


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


class _FakeMessage:
    def __init__(self, content: str) -> None:
        self.content = content


class _FakeChoice:
    def __init__(self, content: str) -> None:
        self.message = _FakeMessage(content)


class _FakeResponse:
    def __init__(self, content: str) -> None:
        self.choices = [_FakeChoice(content)]


async def test_generate_prompt_returns_model_output(
    make_user_and_token, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, token = await make_user_and_token(TENANT_A, role="developer")

    captured: dict[str, Any] = {}

    async def _fake_acompletion(**kwargs: Any) -> _FakeResponse:
        captured.update(kwargs)
        return _FakeResponse("  You are {{agent_name}} for {{company_name}}.  ")

    monkeypatch.setattr(litellm, "acompletion", _fake_acompletion)

    with TestClient(app) as client:
        response = client.post(
            "/api/v1/agents/generate-prompt",
            json={
                "agent_name": "KYC Agent",
                "business_purpose": "Automate KYC document verification",
                "agent_type": "standard",
                "tools": ["Companies House Lookup"],
                "tone": "professional",
                "target_audience": "compliance_officers",
            },
            headers=_bearer(token),
        )

    assert response.status_code == 200
    body = response.json()
    # Stripped of surrounding whitespace, never a raw/untrimmed LLM reply.
    assert body["system_prompt"] == "You are {{agent_name}} for {{company_name}}."
    # Factory-internal call — always Bedrock via the model-prefix convention,
    # never anything derived from a to-be-created agent's own model config.
    assert captured["model"].startswith("bedrock/")
    assert "KYC Agent" in captured["messages"][1]["content"]
    assert "Companies House Lookup" in captured["messages"][1]["content"]


async def test_improve_prompt_returns_model_output(
    make_user_and_token, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, token = await make_user_and_token(TENANT_A, role="developer")

    async def _fake_acompletion(**kwargs: Any) -> _FakeResponse:
        return _FakeResponse("You are a concise KYC agent with escalation rules.")

    monkeypatch.setattr(litellm, "acompletion", _fake_acompletion)

    with TestClient(app) as client:
        response = client.post(
            "/api/v1/agents/improve-prompt",
            json={
                "current_prompt": "You are a KYC agent.",
                "improvement_instructions": "Make it more concise and add escalation rules.",
            },
            headers=_bearer(token),
        )

    assert response.status_code == 200
    assert response.json()["system_prompt"] == (
        "You are a concise KYC agent with escalation rules."
    )


async def test_generate_prompt_model_failure_returns_structured_502(
    make_user_and_token, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, token = await make_user_and_token(TENANT_A, role="developer")

    async def _fail(**kwargs: Any) -> None:
        raise RuntimeError("model provider unreachable")

    monkeypatch.setattr(litellm, "acompletion", _fail)

    with TestClient(app) as client:
        response = client.post(
            "/api/v1/agents/generate-prompt",
            json={"agent_name": "Agent", "business_purpose": "Do things"},
            headers=_bearer(token),
        )

    assert response.status_code == 502
    assert response.json()["detail"]["error"] == "prompt_generation_failed"


async def test_generate_prompt_requires_authentication() -> None:
    with TestClient(app) as client:
        response = client.post(
            "/api/v1/agents/generate-prompt",
            json={"agent_name": "Agent", "business_purpose": "Do things"},
        )

    assert response.status_code in (401, 403)
