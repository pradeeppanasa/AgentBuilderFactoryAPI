"""API tests for POST /api/v1/platform/task-planner/analyze-architecture
(CLAUDE.md Section 38.6 "Design corrections" — Wizard Redesign, 2026-08-18).

This is the multi-agent architecture proposal endpoint, added alongside the
existing single-agent /analyze endpoint (test_task_planner_api.py) rather
than in place of it — see the module comment in
app/modules/task_planner/models.py for why both coexist. Same
litellm.acompletion monkeypatch convention as test_task_planner_api.py /
test_model_router.py — no real model call.
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


def _resource(name: str, *, in_catalog: bool, resource_id: str | None) -> dict[str, Any]:
    return {
        "name": name,
        "description": f"{name} description",
        "in_catalog": in_catalog,
        "resource_id": resource_id,
    }


def _agent_proposal(
    name: str,
    *,
    agent_type: str = "standard",
    tools: list[dict[str, Any]] | None = None,
    knowledge_bases: list[dict[str, Any]] | None = None,
    guardrail_policy: dict[str, Any] | None = None,
    capability_description: str = "",
) -> dict[str, Any]:
    return {
        "name": name,
        "description": f"{name} does the work.",
        "agent_type": agent_type,
        "persona_name": None,
        "system_prompt": f"You are {name}.",
        "capability_description": capability_description,
        "tools": tools or [],
        "knowledge_bases": knowledge_bases or [],
        "guardrail_policy": guardrail_policy,
        "skills": [],
    }


def _create_connector(client: TestClient, token: str, name: str = "Companies House") -> str:
    created = client.post(
        "/api/v1/connectors",
        json={"name": name, "executor_type": "http", "description": "d"},
        headers=_bearer(token),
    )
    assert created.status_code == 201
    return created.json()["connector_id"]


async def test_multi_agent_proposal_returned(
    make_user_and_token, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A genuinely multi-agent requirement comes back as an orchestrator
    plus its specialist sub-agents, not flattened into one agent."""
    _, dev_token = await make_user_and_token(TENANT_A, role="developer")

    with TestClient(app) as client:
        payload = {
            "orchestrator": _agent_proposal("KYB Orchestrator", agent_type="orchestrator"),
            "sub_agents": [
                _agent_proposal(
                    "Document Extractor",
                    agent_type="standard",
                    capability_description="Extracts fields from uploaded documents.",
                ),
                _agent_proposal(
                    "Risk Scorer",
                    agent_type="standard",
                    capability_description="Scores business risk from structured data.",
                ),
            ],
            "output_schema": None,
            "confidence": 0.75,
            "reasoning": "Needs document extraction and risk scoring as separate concerns.",
        }

        async def _fake_acompletion(**kwargs: Any) -> _FakeResponse:
            return _FakeResponse(json.dumps(payload))

        monkeypatch.setattr(litellm, "acompletion", _fake_acompletion)

        response = client.post(
            "/api/v1/platform/task-planner/analyze-architecture",
            json={"description": "Verify a new business: extract documents and score risk."},
            headers=_bearer(dev_token),
        )
        assert response.status_code == 200
        body = response.json()

        assert body["orchestrator"]["name"] == "KYB Orchestrator"
        assert body["orchestrator"]["agent_type"] == "orchestrator"
        assert len(body["sub_agents"]) == 2
        assert {a["name"] for a in body["sub_agents"]} == {"Document Extractor", "Risk Scorer"}
        assert body["sub_agents"][0]["capability_description"]


async def test_simple_requirement_single_agent(
    make_user_and_token, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A simple requirement comes back as one "orchestrator" agent
    (agent_type standard) with an empty sub_agents list — there is no
    separate single-agent response shape; zero sub-agents *is* that case."""
    _, dev_token = await make_user_and_token(TENANT_A, role="developer")

    with TestClient(app) as client:
        payload = {
            "orchestrator": _agent_proposal("FAQ Agent", agent_type="standard"),
            "sub_agents": [],
            "output_schema": None,
            "confidence": 0.9,
            "reasoning": "A single agent can answer FAQs end to end.",
        }

        async def _fake_acompletion(**kwargs: Any) -> _FakeResponse:
            return _FakeResponse(json.dumps(payload))

        monkeypatch.setattr(litellm, "acompletion", _fake_acompletion)

        response = client.post(
            "/api/v1/platform/task-planner/analyze-architecture",
            json={"description": "Answer frequently asked questions about our product."},
            headers=_bearer(dev_token),
        )
        assert response.status_code == 200
        body = response.json()

        assert body["orchestrator"]["agent_type"] == "standard"
        assert body["sub_agents"] == []
        assert body["resources"]["tools"] == []


async def test_hallucinated_tool_force_demoted(
    make_user_and_token, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Even if a sub-agent's proposal claims a resource_id that isn't in
    the real tenant catalog, it must come back demoted to
    in_catalog=false/resource_id=None — same defense-in-depth guarantee as
    the single-agent endpoint, applied per-agent here."""
    _, dev_token = await make_user_and_token(TENANT_A, role="developer")

    with TestClient(app) as client:
        payload = {
            "orchestrator": _agent_proposal("Orchestrator", agent_type="orchestrator"),
            "sub_agents": [
                _agent_proposal(
                    "Risk Scorer",
                    tools=[
                        _resource(
                            "D&B Risk Score", in_catalog=True, resource_id="hallucinated-id"
                        )
                    ],
                )
            ],
            "output_schema": None,
            "confidence": 0.6,
            "reasoning": "r",
        }

        async def _fake_acompletion(**kwargs: Any) -> _FakeResponse:
            return _FakeResponse(json.dumps(payload))

        monkeypatch.setattr(litellm, "acompletion", _fake_acompletion)

        response = client.post(
            "/api/v1/platform/task-planner/analyze-architecture",
            json={"description": "Score business risk."},
            headers=_bearer(dev_token),
        )
        assert response.status_code == 200
        body = response.json()

        sub_tool = body["sub_agents"][0]["tools"][0]
        assert sub_tool["in_catalog"] is False
        assert sub_tool["resource_id"] is None
        # The aggregated resources view must reflect the same demotion.
        assert body["resources"]["tools"][0]["in_catalog"] is False


async def test_existing_resource_returned_with_id(
    make_user_and_token, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A resource that genuinely exists in the tenant's catalog is returned
    with in_catalog=true and its real resource_id preserved, and appears
    (deduplicated) in the aggregated `resources` view."""
    _, dev_token = await make_user_and_token(TENANT_A, role="developer")

    with TestClient(app) as client:
        tool_id = _create_connector(client, dev_token)

        payload = {
            "orchestrator": _agent_proposal("Orchestrator", agent_type="orchestrator"),
            "sub_agents": [
                _agent_proposal(
                    "Verifier",
                    tools=[_resource("Companies House", in_catalog=True, resource_id=tool_id)],
                )
            ],
            "output_schema": None,
            "confidence": 0.8,
            "reasoning": "r",
        }

        async def _fake_acompletion(**kwargs: Any) -> _FakeResponse:
            return _FakeResponse(json.dumps(payload))

        monkeypatch.setattr(litellm, "acompletion", _fake_acompletion)

        response = client.post(
            "/api/v1/platform/task-planner/analyze-architecture",
            json={"description": "Verify UK business customers against Companies House."},
            headers=_bearer(dev_token),
        )
        assert response.status_code == 200
        body = response.json()

        assert body["sub_agents"][0]["tools"][0]["resource_id"] == tool_id
        assert body["sub_agents"][0]["tools"][0]["in_catalog"] is True
        assert body["resources"]["tools"] == [
            {
                "name": "Companies House",
                "description": "Companies House description",
                "in_catalog": True,
                "resource_id": tool_id,
            }
        ]


async def test_malformed_json_returns_502(
    make_user_and_token, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Both attempts returning unparseable output is a clean 502, matching
    the single-agent endpoint's error contract — never a bare 500 or a
    silently-guessed partial proposal."""
    _, dev_token = await make_user_and_token(TENANT_A, role="developer")

    with TestClient(app) as client:

        async def _fake_acompletion(**kwargs: Any) -> _FakeResponse:
            return _FakeResponse("not json at all")

        monkeypatch.setattr(litellm, "acompletion", _fake_acompletion)

        response = client.post(
            "/api/v1/platform/task-planner/analyze-architecture",
            json={"description": "Do something."},
            headers=_bearer(dev_token),
        )
        assert response.status_code == 502
        assert "architecture proposal" in response.json()["detail"]
