"""API tests for POST /api/v1/platform/task-planner/analyze (CLAUDE.md
Section 38.6 Step 1 / 38.7 — A2-3).

litellm.acompletion is monkeypatched per-test (matches test_model_router.py /
test_playground_api.py's convention — no real model call). Response shape
matches panasa-agent-builder-ui/src/types/task-planner.ts exactly (the UI
was built first, against CLAUDE.md Section 38.6's proposal card layout,
which shows a single suggested guardrail policy rather than a list) — the
core guarantee under test is catalog-boundedness: even when the fake LLM
response claims a resource_id that isn't in the tenant's real catalog, the
API must demote it to in_catalog=false/resource_id=None rather than pass
it through.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any

import litellm
import pytest
from fastapi.testclient import TestClient

from app.dependencies import get_bedrock_guardrail_provisioner
from app.main import app
from app.modules.guardrails.provisioner import BedrockGuardrailProvisioner
from tests.fakes import FakeBedrockControlPlaneClient

TENANT_A = "tenant-a"
TENANT_B = "tenant-b"


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


@pytest.fixture(autouse=True)
def fake_bedrock_provisioner() -> Iterator[FakeBedrockControlPlaneClient]:
    client = FakeBedrockControlPlaneClient()
    app.dependency_overrides[get_bedrock_guardrail_provisioner] = (
        lambda: BedrockGuardrailProvisioner(client)
    )
    try:
        yield client
    finally:
        del app.dependency_overrides[get_bedrock_guardrail_provisioner]


def _valid_proposal_json(
    *, tool_id: str, kb_id: str, guardrail_id: str | None = None
) -> str:
    return json.dumps(
        {
            "suggested_name": "KYB Verification Agent",
            "suggested_description": "Verifies UK business customers against Companies House.",
            "suggested_agent_type": "task",
            "suggested_persona_name": "KYB verification specialist",
            "suggested_system_prompt": "You verify UK business customers...",
            "suggested_tools": [
                {
                    "name": "Companies House",
                    "description": "Needed to verify UK company registration.",
                    "in_catalog": True,
                    "resource_id": tool_id,
                },
                {
                    "name": "D&B Risk Score",
                    "description": "No risk-scoring connector exists yet.",
                    "in_catalog": False,
                    "resource_id": None,
                },
            ],
            "suggested_knowledge_bases": [
                {
                    "name": "KYB Policy Library",
                    "description": "Contains KYB verification policy.",
                    "in_catalog": True,
                    "resource_id": kb_id,
                }
            ],
            "suggested_guardrail_policy": (
                {
                    "name": "Financial Compliance Policy",
                    "description": "Applies to financial verification agents.",
                    "in_catalog": guardrail_id is not None,
                    "resource_id": guardrail_id,
                }
                if guardrail_id is not None
                else None
            ),
            "suggested_skills": [],
            "suggested_output_format": "structured JSON verification result",
            "confidence": 0.82,
            "reasoning": "Matched existing Companies House connector and KB.",
        }
    )


def _create_connector(client: TestClient, token: str) -> str:
    created = client.post(
        "/api/v1/connectors",
        json={
            "name": "Companies House",
            "executor_type": "http",
            "description": "UK company registry lookup",
        },
        headers=_bearer(token),
    )
    assert created.status_code == 201
    return created.json()["connector_id"]


def _create_kb(client: TestClient, token: str) -> str:
    created = client.post(
        "/api/v1/platform/knowledge-bases",
        json={
            "name": "KYB Policy Library",
            "description": "KYB verification policy documents",
            "source_type": "manual",
        },
        headers=_bearer(token),
    )
    assert created.status_code == 201
    return created.json()["kb_id"]


async def test_proposal_marks_real_catalog_resources_available(
    make_user_and_token, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, dev_token = await make_user_and_token(TENANT_A, role="developer")

    with TestClient(app) as client:
        tool_id = _create_connector(client, dev_token)
        kb_id = _create_kb(client, dev_token)

        async def _fake_acompletion(**kwargs: Any) -> _FakeResponse:
            return _FakeResponse(_valid_proposal_json(tool_id=tool_id, kb_id=kb_id))

        monkeypatch.setattr(litellm, "acompletion", _fake_acompletion)

        response = client.post(
            "/api/v1/platform/task-planner/analyze",
            json={"description": "Verify new UK business customers against Companies House."},
            headers=_bearer(dev_token),
        )
        assert response.status_code == 200
        body = response.json()

        assert body["suggested_name"] == "KYB Verification Agent"
        assert body["suggested_description"]
        assert body["suggested_agent_type"] == "task"
        assert body["suggested_persona_name"] == "KYB verification specialist"
        assert body["suggested_output_format"] == "structured JSON verification result"

        assert body["suggested_tools"][0]["resource_id"] == tool_id
        assert body["suggested_tools"][0]["in_catalog"] is True
        assert body["suggested_tools"][1]["resource_id"] is None
        assert body["suggested_tools"][1]["in_catalog"] is False

        assert body["suggested_knowledge_bases"][0]["resource_id"] == kb_id
        assert body["suggested_knowledge_bases"][0]["in_catalog"] is True

        assert body["suggested_guardrail_policy"] is None
        assert body["suggested_skills"] == []


async def test_suggested_guardrail_policy_is_a_single_object(
    make_user_and_token, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, dev_token = await make_user_and_token(TENANT_A, role="developer")
    _, admin_token = await make_user_and_token(TENANT_A, role="admin")

    with TestClient(app) as client:
        tool_id = _create_connector(client, dev_token)
        kb_id = _create_kb(client, dev_token)
        # Guardrail policy creation is admin-only (Section 37.7: "Policies
        # are created by admins only. Developers pick from the policy
        # library.") — dev_token would 403 here.
        created_policy = client.post(
            "/api/v1/platform/guardrail-policies",
            json={"name": "Financial Compliance Policy", "description": "d"},
            headers=_bearer(admin_token),
        )
        assert created_policy.status_code == 201
        guardrail_id = created_policy.json()["policy_id"]

        async def _fake_acompletion(**kwargs: Any) -> _FakeResponse:
            return _FakeResponse(
                _valid_proposal_json(tool_id=tool_id, kb_id=kb_id, guardrail_id=guardrail_id)
            )

        monkeypatch.setattr(litellm, "acompletion", _fake_acompletion)

        response = client.post(
            "/api/v1/platform/task-planner/analyze",
            json={"description": "Verify new UK business customers against Companies House."},
            headers=_bearer(dev_token),
        )
        assert response.status_code == 200
        guardrail = response.json()["suggested_guardrail_policy"]
        assert isinstance(guardrail, dict)
        assert guardrail["resource_id"] == guardrail_id
        assert guardrail["in_catalog"] is True


async def test_hallucinated_resource_id_is_demoted_to_not_in_catalog(
    make_user_and_token, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Even if the LLM disobeys the system prompt and invents a resource_id
    that isn't in the real catalog, the API must never pass it through as
    in_catalog=true — this is the defense-in-depth check, not just prompt
    instructions."""
    _, dev_token = await make_user_and_token(TENANT_A, role="developer")

    with TestClient(app) as client:

        async def _fake_acompletion(**kwargs: Any) -> _FakeResponse:
            return _FakeResponse(
                _valid_proposal_json(
                    tool_id="hallucinated-id-999",
                    kb_id="also-fake",
                    guardrail_id="fake-guardrail-id",
                )
            )

        monkeypatch.setattr(litellm, "acompletion", _fake_acompletion)

        response = client.post(
            "/api/v1/platform/task-planner/analyze",
            json={"description": "Verify new UK business customers."},
            headers=_bearer(dev_token),
        )
        assert response.status_code == 200
        body = response.json()

        assert body["suggested_tools"][0]["resource_id"] is None
        assert body["suggested_tools"][0]["in_catalog"] is False
        assert body["suggested_knowledge_bases"][0]["resource_id"] is None
        assert body["suggested_knowledge_bases"][0]["in_catalog"] is False
        assert body["suggested_guardrail_policy"]["resource_id"] is None
        assert body["suggested_guardrail_policy"]["in_catalog"] is False


async def test_catalog_is_tenant_scoped(
    make_user_and_token, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A resource created under tenant A must not be treated as available
    when tenant B's Task Planner call claims that same id."""
    _, dev_a_token = await make_user_and_token(TENANT_A, role="developer")
    _, dev_b_token = await make_user_and_token(TENANT_B, role="developer")

    with TestClient(app) as client:
        tool_id = _create_connector(client, dev_a_token)

        async def _fake_acompletion(**kwargs: Any) -> _FakeResponse:
            return _FakeResponse(_valid_proposal_json(tool_id=tool_id, kb_id="none"))

        monkeypatch.setattr(litellm, "acompletion", _fake_acompletion)

        response = client.post(
            "/api/v1/platform/task-planner/analyze",
            json={"description": "Verify new UK business customers."},
            headers=_bearer(dev_b_token),
        )
        assert response.status_code == 200
        body = response.json()
        assert body["suggested_tools"][0]["resource_id"] is None
        assert body["suggested_tools"][0]["in_catalog"] is False


async def test_invalid_json_retries_once_then_succeeds(
    make_user_and_token, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, dev_token = await make_user_and_token(TENANT_A, role="developer")

    with TestClient(app) as client:
        tool_id = _create_connector(client, dev_token)
        calls = {"count": 0}

        async def _fake_acompletion(**kwargs: Any) -> _FakeResponse:
            calls["count"] += 1
            if calls["count"] == 1:
                return _FakeResponse("not json at all, sorry")
            return _FakeResponse(_valid_proposal_json(tool_id=tool_id, kb_id="none"))

        monkeypatch.setattr(litellm, "acompletion", _fake_acompletion)

        response = client.post(
            "/api/v1/platform/task-planner/analyze",
            json={"description": "Verify new UK business customers."},
            headers=_bearer(dev_token),
        )
        assert response.status_code == 200
        assert calls["count"] == 2
        assert response.json()["suggested_tools"][0]["resource_id"] == tool_id


async def test_invalid_json_both_attempts_returns_502(
    make_user_and_token, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, dev_token = await make_user_and_token(TENANT_A, role="developer")

    with TestClient(app) as client:

        async def _fake_acompletion(**kwargs: Any) -> _FakeResponse:
            return _FakeResponse("still not json")

        monkeypatch.setattr(litellm, "acompletion", _fake_acompletion)

        response = client.post(
            "/api/v1/platform/task-planner/analyze",
            json={"description": "Verify new UK business customers."},
            headers=_bearer(dev_token),
        )
        assert response.status_code == 502


async def test_model_call_failure_returns_502_not_500(
    make_user_and_token, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A raw LiteLLM/Bedrock failure (auth, throttling, timeout, model not
    found) must surface as a clean 502 with detail, not an opaque 500 —
    this is exactly what happens against real Bedrock when local dev's
    placeholder AWS credentials are used (CLAUDE.md Section 34)."""
    _, dev_token = await make_user_and_token(TENANT_A, role="developer")

    with TestClient(app) as client:

        async def _fake_acompletion(**kwargs: Any) -> _FakeResponse:
            raise litellm.exceptions.AuthenticationError(
                message="Invalid Authentication",
                llm_provider="bedrock",
                model="anthropic.claude-3-5-haiku-20241022-v1:0",
            )

        monkeypatch.setattr(litellm, "acompletion", _fake_acompletion)

        response = client.post(
            "/api/v1/platform/task-planner/analyze",
            json={"description": "Verify new UK business customers."},
            headers=_bearer(dev_token),
        )
        assert response.status_code == 502
        assert "Task Planner's model call failed" in response.json()["detail"]


async def test_markdown_fenced_json_is_parsed(
    make_user_and_token, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, dev_token = await make_user_and_token(TENANT_A, role="developer")

    with TestClient(app) as client:
        tool_id = _create_connector(client, dev_token)

        async def _fake_acompletion(**kwargs: Any) -> _FakeResponse:
            fenced = f"```json\n{_valid_proposal_json(tool_id=tool_id, kb_id='none')}\n```"
            return _FakeResponse(fenced)

        monkeypatch.setattr(litellm, "acompletion", _fake_acompletion)

        response = client.post(
            "/api/v1/platform/task-planner/analyze",
            json={"description": "Verify new UK business customers."},
            headers=_bearer(dev_token),
        )
        assert response.status_code == 200
        assert response.json()["suggested_tools"][0]["resource_id"] == tool_id


async def test_auditor_can_call_analyze(
    make_user_and_token, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Read-only relative to persisted state — open to every authenticated
    role, not just developer."""
    _, auditor_token = await make_user_and_token(TENANT_A, role="auditor")

    with TestClient(app) as client:

        async def _fake_acompletion(**kwargs: Any) -> _FakeResponse:
            return _FakeResponse(_valid_proposal_json(tool_id="none", kb_id="none"))

        monkeypatch.setattr(litellm, "acompletion", _fake_acompletion)

        response = client.post(
            "/api/v1/platform/task-planner/analyze",
            json={"description": "Verify new UK business customers."},
            headers=_bearer(auditor_token),
        )
        assert response.status_code == 200


async def test_analyze_requires_authentication(make_user_and_token) -> None:
    with TestClient(app) as client:
        response = client.post(
            "/api/v1/platform/task-planner/analyze",
            json={"description": "Verify new UK business customers."},
        )
        assert response.status_code in (401, 403)
