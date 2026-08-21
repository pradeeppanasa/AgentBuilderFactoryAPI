"""API tests for the "Build with AI" propose/approve/status flow
(CLAUDE.md Section 42). Same litellm.acompletion monkeypatch convention as
test_task_planner_architecture_api.py — no real model call. Propose makes
two sequential model calls (architecture, then missing-resource config
elaboration), so the fake here returns a different payload per call.
"""

from __future__ import annotations

import json
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
    skills: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return {
        "name": name,
        "description": f"{name} does the work.",
        "agent_type": agent_type,
        "persona_name": None,
        "system_prompt": f"You are {name}.",
        "capability_description": "",
        "tools": tools or [],
        "knowledge_bases": knowledge_bases or [],
        "guardrail_policy": None,
        "skills": skills or [],
    }


def _create_connector(client: TestClient, token: str) -> str:
    response = client.post(
        "/api/v1/connectors",
        json={
            "name": "Companies House Lookup",
            "executor_type": "http",
            "description": "Look up company registration data",
            "input_schema": {"type": "object"},
            "output_schema": {"type": "object"},
        },
        headers=_bearer(token),
    )
    assert response.status_code == 201
    return str(response.json()["connector_id"])


def _mock_architecture_and_configs(
    monkeypatch: pytest.MonkeyPatch,
    architecture_payload: dict[str, Any],
    config_payload: dict[str, Any] | None = None,
) -> None:
    call_count = {"n": 0}

    async def _fake_acompletion(**kwargs: Any) -> _FakeResponse:
        call_count["n"] += 1
        if call_count["n"] == 1:
            return _FakeResponse(json.dumps(architecture_payload))
        return _FakeResponse(json.dumps(config_payload or {"resources": []}))

    monkeypatch.setattr(litellm, "acompletion", _fake_acompletion)


async def test_propose_classifies_available_and_missing_resources(
    make_user_and_token, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, dev_token = await make_user_and_token(TENANT_A, role="developer")

    with TestClient(app) as client:
        tool_id = _create_connector(client, dev_token)

        architecture = {
            "orchestrator": _agent_proposal(
                "Payroll Verification Agent",
                tools=[_resource("Companies House Lookup", in_catalog=True, resource_id=tool_id)],
                skills=[_resource("Payroll Compliance Skill", in_catalog=False, resource_id=None)],
            ),
            "sub_agents": [],
            "output_schema": None,
            "confidence": 0.8,
            "reasoning": "One agent can handle this end to end.",
        }
        config = {
            "resources": [
                {
                    "resource_key": "skill:payroll-compliance-skill",
                    "proposed_config": {
                        "capability": "Check payroll compliance",
                        "prompt_fragment": "Use this to verify payroll compliance rules.",
                    },
                }
            ]
        }
        _mock_architecture_and_configs(monkeypatch, architecture, config)

        response = client.post(
            "/api/v1/agents/build-with-ai/propose",
            json={"description": "Verify payroll compliance for employees."},
            headers=_bearer(dev_token),
        )

    assert response.status_code == 200
    body = response.json()
    assert body["session_id"]
    assert body["available_resources"] == [
        {"resource_type": "tool", "resource_id": tool_id, "name": "Companies House Lookup"}
    ]
    assert len(body["missing_resources"]) == 1
    missing = body["missing_resources"][0]
    assert missing["resource_type"] == "skill"
    assert missing["resource_key"] == "skill:payroll-compliance-skill"
    assert missing["proposed_config"]["capability"] == "Check payroll compliance"
    assert len(body["proposed_agents"]) == 1
    assert body["proposed_agents"][0]["name"] == "Payroll Verification Agent"

    # R48 — never a credential value anywhere in the proposal.
    assert "credential" not in response.text.lower()
    assert "secret" not in response.text.lower()


async def test_approve_creates_missing_resource_and_agent(
    make_user_and_token, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, dev_token = await make_user_and_token(TENANT_A, role="developer")

    with TestClient(app) as client:
        architecture = {
            "orchestrator": _agent_proposal(
                "Payroll Verification Agent",
                skills=[_resource("Payroll Compliance Skill", in_catalog=False, resource_id=None)],
            ),
            "sub_agents": [],
            "output_schema": None,
            "confidence": 0.8,
            "reasoning": "One agent can handle this end to end.",
        }
        _mock_architecture_and_configs(monkeypatch, architecture)

        proposed = client.post(
            "/api/v1/agents/build-with-ai/propose",
            json={"description": "Verify payroll compliance for employees."},
            headers=_bearer(dev_token),
        ).json()
        session_id = proposed["session_id"]

        approved = client.post(
            "/api/v1/agents/build-with-ai/approve",
            json={"session_id": session_id},
            headers=_bearer(dev_token),
        )
        assert approved.status_code == 200
        approve_body = approved.json()
        assert len(approve_body["created_resources"]) == 1
        assert approve_body["created_resources"][0]["resource_type"] == "skill"
        assert approve_body["skipped_resource_keys"] == []
        assert len(approve_body["created_agents"]) == 1
        agent_id = approve_body["created_agents"][0]["agent_id"]

        # The agent really exists, with the new skill attached.
        agent_response = client.get(f"/api/v1/agents/{agent_id}", headers=_bearer(dev_token))
        assert agent_response.status_code == 200
        skill_ids = [s["skill_id"] for s in agent_response.json()["configuration"]["skills"]]
        assert skill_ids == [approve_body["created_resources"][0]["resource_id"]]

        status_response = client.get(
            f"/api/v1/agents/build-with-ai/{session_id}/status",
            headers=_bearer(dev_token),
        )
        assert status_response.status_code == 200
        assert status_response.json()["status"] == "completed"


async def test_approve_skips_resource_marked_skip(
    make_user_and_token, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Section 42.3 — "Skip" removes the resource from the proposed
    architecture entirely; nothing is created for it and the agent that
    would have used it just doesn't get it attached."""
    _, dev_token = await make_user_and_token(TENANT_A, role="developer")

    with TestClient(app) as client:
        architecture = {
            "orchestrator": _agent_proposal(
                "Payroll Verification Agent",
                skills=[_resource("Payroll Compliance Skill", in_catalog=False, resource_id=None)],
            ),
            "sub_agents": [],
            "output_schema": None,
            "confidence": 0.8,
            "reasoning": "One agent can handle this end to end.",
        }
        _mock_architecture_and_configs(monkeypatch, architecture)

        proposed = client.post(
            "/api/v1/agents/build-with-ai/propose",
            json={"description": "Verify payroll compliance for employees."},
            headers=_bearer(dev_token),
        ).json()
        session_id = proposed["session_id"]
        resource_key = proposed["missing_resources"][0]["resource_key"]

        approved = client.post(
            "/api/v1/agents/build-with-ai/approve",
            json={"session_id": session_id, "skip_resource_keys": [resource_key]},
            headers=_bearer(dev_token),
        )
        assert approved.status_code == 200
        approve_body = approved.json()
        assert approve_body["created_resources"] == []
        assert approve_body["skipped_resource_keys"] == [resource_key]

        agent_id = approve_body["created_agents"][0]["agent_id"]
        agent_response = client.get(f"/api/v1/agents/{agent_id}", headers=_bearer(dev_token))
        assert agent_response.json()["configuration"]["skills"] == []


async def test_approve_never_returns_a_credential_value(
    make_user_and_token, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R48 — a "tool" resource may carry an auth_type label, never a
    credential value, anywhere in the propose/approve response bodies."""
    _, dev_token = await make_user_and_token(TENANT_A, role="developer")

    with TestClient(app) as client:
        architecture = {
            "orchestrator": _agent_proposal(
                "Payroll Verification Agent",
                tools=[_resource("Employee Payroll API", in_catalog=False, resource_id=None)],
            ),
            "sub_agents": [],
            "output_schema": None,
            "confidence": 0.8,
            "reasoning": "One agent can handle this end to end.",
        }
        config = {
            "resources": [
                {
                    "resource_key": "tool:employee-payroll-api",
                    "proposed_config": {
                        "purpose": "Retrieve employee payroll records",
                        "endpoint": "https://api.company.com/payroll/v1/employees/{id}",
                        "http_method": "GET",
                        "auth_type": "bearer",
                        "executor_type": "http",
                        "input_schema": {"type": "object"},
                        "output_schema": {"type": "object"},
                    },
                }
            ]
        }
        _mock_architecture_and_configs(monkeypatch, architecture, config)

        proposed_response = client.post(
            "/api/v1/agents/build-with-ai/propose",
            json={"description": "Retrieve employee payroll records."},
            headers=_bearer(dev_token),
        )
        assert "credential" not in proposed_response.text.lower()
        session_id = proposed_response.json()["session_id"]

        approved = client.post(
            "/api/v1/agents/build-with-ai/approve",
            json={"session_id": session_id},
            headers=_bearer(dev_token),
        )
        assert approved.status_code == 200
        assert "credential" not in approved.text.lower()

        agent_id = approved.json()["created_agents"][0]["agent_id"]
        agent_response = client.get(f"/api/v1/agents/{agent_id}", headers=_bearer(dev_token))
        tool_instances = agent_response.json()["configuration"]["tool_instances"]
        assert len(tool_instances) == 1


async def test_approve_unknown_session_returns_404(make_user_and_token) -> None:
    _, dev_token = await make_user_and_token(TENANT_A, role="developer")

    with TestClient(app) as client:
        response = client.post(
            "/api/v1/agents/build-with-ai/approve",
            json={"session_id": "bwai-does-not-exist"},
            headers=_bearer(dev_token),
        )
    assert response.status_code == 404


async def test_status_unknown_session_returns_404(make_user_and_token) -> None:
    _, dev_token = await make_user_and_token(TENANT_A, role="developer")

    with TestClient(app) as client:
        response = client.get(
            "/api/v1/agents/build-with-ai/bwai-does-not-exist/status",
            headers=_bearer(dev_token),
        )
    assert response.status_code == 404


async def test_approve_twice_returns_409_and_does_not_double_create(
    make_user_and_token, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A session is a one-time-use computation (CLAUDE.md Section 42.1 rule
    3/R47) — approving it again must not silently re-run execute_approval
    and mint a second copy of every resource/agent."""
    _, dev_token = await make_user_and_token(TENANT_A, role="developer")

    with TestClient(app) as client:
        architecture = {
            "orchestrator": _agent_proposal(
                "Payroll Verification Agent",
                skills=[_resource("Payroll Compliance Skill", in_catalog=False, resource_id=None)],
            ),
            "sub_agents": [],
            "output_schema": None,
            "confidence": 0.8,
            "reasoning": "One agent can handle this end to end.",
        }
        _mock_architecture_and_configs(monkeypatch, architecture)

        proposed = client.post(
            "/api/v1/agents/build-with-ai/propose",
            json={"description": "Verify payroll compliance for employees."},
            headers=_bearer(dev_token),
        ).json()
        session_id = proposed["session_id"]

        first = client.post(
            "/api/v1/agents/build-with-ai/approve",
            json={"session_id": session_id},
            headers=_bearer(dev_token),
        )
        assert first.status_code == 200

        second = client.post(
            "/api/v1/agents/build-with-ai/approve",
            json={"session_id": session_id},
            headers=_bearer(dev_token),
        )
        assert second.status_code == 409


async def test_status_is_proposed_before_approve(
    make_user_and_token, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, dev_token = await make_user_and_token(TENANT_A, role="developer")

    with TestClient(app) as client:
        architecture = {
            "orchestrator": _agent_proposal("Simple Agent"),
            "sub_agents": [],
            "output_schema": None,
            "confidence": 0.9,
            "reasoning": "Trivial single-agent case.",
        }
        _mock_architecture_and_configs(monkeypatch, architecture)

        proposed = client.post(
            "/api/v1/agents/build-with-ai/propose",
            json={"description": "Do something simple."},
            headers=_bearer(dev_token),
        ).json()

        status_response = client.get(
            f"/api/v1/agents/build-with-ai/{proposed['session_id']}/status",
            headers=_bearer(dev_token),
        )
    assert status_response.status_code == 200
    assert status_response.json()["status"] == "proposed"
