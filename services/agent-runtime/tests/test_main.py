"""main.py loads its config and builds AgentOrchestrator at import time
(module-level, by design — "load config once on startup", same as the
instruction's own skeleton) — so config_loader.load_agent_config must be
patched BEFORE main is first imported in this process. sub-components
(LLMClient, RAGClient, …) are safe to construct for real here: building a
boto3 client object never makes a network call or needs real credentials,
only actually *calling* one does — and these tests never call /chat
without first replacing orchestrator.run with a fake."""

from __future__ import annotations

import sys
from typing import Any

import pytest
from fastapi.testclient import TestClient


def _fresh_main(monkeypatch: pytest.MonkeyPatch, config: dict[str, Any]) -> Any:
    monkeypatch.setenv("AGENT_ID", config["agent_id"])
    monkeypatch.setenv("TENANT_ID", config["tenant_id"])

    import config_loader

    monkeypatch.setattr(config_loader, "load_agent_config", lambda dynamodb=None: config)

    sys.modules.pop("main", None)
    import main as main_module

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


def test_health_returns_agent_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    main_module = _fresh_main(monkeypatch, _config())

    with TestClient(main_module.app) as client:
        response = client.get("/health")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "healthy"
    assert body["agent_id"] == "faq-agent-1"
    assert body["agent_name"] == "FAQ Agent"


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
    main_module = _fresh_main(monkeypatch, _config())

    async def _fake_run(message: str, session_id: str, user_id: str | None = None) -> dict[str, Any]:
        return {
            "response": "The refund window is 30 days.",
            "session_id": session_id,
            "run_id": "run-123",
            "hitl_pending": False,
        }

    monkeypatch.setattr(main_module.orchestrator, "run", _fake_run)

    with TestClient(main_module.app) as client:
        response = client.post(
            "/chat", json={"message": "What's your refund policy?", "session_id": "s1"}
        )

    assert response.status_code == 200
    body = response.json()
    assert body["response"] == "The refund window is 30 days."
    assert body["run_id"] == "run-123"


def test_chat_returns_500_with_no_internal_detail_on_orchestrator_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    main_module = _fresh_main(monkeypatch, _config())

    async def _failing_run(message: str, session_id: str, user_id: str | None = None) -> dict[str, Any]:
        raise RuntimeError("Bedrock threw a very specific internal exception with sensitive detail")

    monkeypatch.setattr(main_module.orchestrator, "run", _failing_run)

    with TestClient(main_module.app) as client:
        response = client.post("/chat", json={"message": "hi", "session_id": "s1"})

    assert response.status_code == 500
    assert "sensitive detail" not in response.text
    assert response.json()["detail"] == "Agent execution failed"


def test_chat_without_api_key_configured_requires_no_auth(monkeypatch: pytest.MonkeyPatch) -> None:
    main_module = _fresh_main(monkeypatch, _config())
    monkeypatch.delenv("AGENT_API_KEY", raising=False)

    async def _fake_run(message: str, session_id: str, user_id: str | None = None) -> dict[str, Any]:
        return {"response": "ok", "session_id": session_id, "run_id": "r1", "hitl_pending": False}

    monkeypatch.setattr(main_module.orchestrator, "run", _fake_run)

    with TestClient(main_module.app) as client:
        response = client.post("/chat", json={"message": "hi", "session_id": "s1"})

    assert response.status_code == 200


def test_chat_with_api_key_configured_rejects_missing_or_wrong_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    main_module = _fresh_main(monkeypatch, _config())
    monkeypatch.setenv("AGENT_API_KEY", "correct-key")

    async def _fake_run(message: str, session_id: str, user_id: str | None = None) -> dict[str, Any]:
        return {"response": "ok", "session_id": session_id, "run_id": "r1", "hitl_pending": False}

    monkeypatch.setattr(main_module.orchestrator, "run", _fake_run)

    with TestClient(main_module.app) as client:
        no_key = client.post("/chat", json={"message": "hi", "session_id": "s1"})
        wrong_key = client.post(
            "/chat",
            json={"message": "hi", "session_id": "s1"},
            headers={"Authorization": "Bearer wrong-key"},
        )
        right_key = client.post(
            "/chat",
            json={"message": "hi", "session_id": "s1"},
            headers={"Authorization": "Bearer correct-key"},
        )

    assert no_key.status_code == 401
    assert wrong_key.status_code == 401
    assert right_key.status_code == 200
