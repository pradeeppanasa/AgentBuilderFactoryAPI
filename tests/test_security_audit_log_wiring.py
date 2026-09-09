"""Sprint 3 Phase 2 (CLAUDE.md Section 61) — verifies agent.created/
agent.updated/agent.deployed actually land in panasa-audit-log via the real
API, the same "hit the real backing store directly" verification pattern
tests/test_audit_log_api.py already uses for the older S3 audit trail.
"""

from __future__ import annotations

from typing import Any

import boto3
from fastapi.testclient import TestClient

from app.config import settings
from app.main import app
from tests.fakes import FakeGitProvider

TENANT_A = "tenant-a"


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _minimal_agent_payload(name: str = "Audit Wiring Agent") -> dict[str, Any]:
    return {
        "name": name,
        "description": "d",
        "business_purpose": "b",
        "agent_type": "standard",
        "configuration": {
            "model_id": "anthropic.claude-3-5-sonnet-20241022-v2:0",
            "model_provider": "bedrock",
            "system_prompt": "You are a test agent.",
        },
    }


def _events_for_agent(tenant_id: str, agent_id: str) -> list[dict[str, Any]]:
    dynamodb = boto3.resource("dynamodb", region_name="eu-west-2")
    table = dynamodb.Table(settings.dynamodb_audit_log_table)
    response = table.query(
        KeyConditionExpression=boto3.dynamodb.conditions.Key("tenant_id").eq(tenant_id)
    )
    return [item for item in response.get("Items", []) if item.get("agent_id") == agent_id]


async def test_create_agent_writes_agent_created_security_audit_event(
    make_user_and_token,
) -> None:
    user, token = await make_user_and_token(TENANT_A, role="developer")

    with TestClient(app) as client:
        created = client.post(
            "/api/v1/agents", json=_minimal_agent_payload(), headers=_bearer(token)
        ).json()

    events = _events_for_agent(TENANT_A, created["agent_id"])
    matching = [e for e in events if e["event_type"] == "agent.created"]
    assert len(matching) == 1
    assert matching[0]["result"] == "success"
    assert matching[0]["principal_id"] == user.email


async def test_update_agent_writes_agent_updated_security_audit_event(
    make_user_and_token,
) -> None:
    _, token = await make_user_and_token(TENANT_A, role="developer")

    with TestClient(app) as client:
        created = client.post(
            "/api/v1/agents", json=_minimal_agent_payload(), headers=_bearer(token)
        ).json()
        agent_id = created["agent_id"]

        payload = _minimal_agent_payload()
        payload["configuration"]["temperature"] = 0.7
        client.put(
            f"/api/v1/agents/{agent_id}",
            json={
                "configuration": payload["configuration"],
                "change_description": "Bump temperature",
            },
            headers=_bearer(token),
        )

    events = _events_for_agent(TENANT_A, agent_id)
    matching = [e for e in events if e["event_type"] == "agent.updated"]
    assert len(matching) == 1
    assert matching[0]["result"] == "success"


async def test_deployment_complete_writes_agent_deployed_security_audit_event(
    monkeypatch, make_user_and_token
) -> None:
    monkeypatch.setattr(settings, "internal_webhook_secret", "test-shared-secret")
    _, token = await make_user_and_token(TENANT_A, role="developer")

    with TestClient(app) as client:
        app.state.git_provider = FakeGitProvider()
        created = client.post(
            "/api/v1/agents", json=_minimal_agent_payload(), headers=_bearer(token)
        ).json()
        agent_id = created["agent_id"]
        deploy = client.post(f"/api/v1/agents/{agent_id}/deploy", headers=_bearer(token)).json()

        client.post(
            "/api/v1/internal/deployment-complete",
            json={
                "agent_id": agent_id,
                "tenant_id": TENANT_A,
                "deployment_id": deploy["deployment_id"],
                "version": deploy["version"],
                "status": "ACTIVE",
            },
            headers={"Authorization": "Bearer test-shared-secret"},
        )

    events = _events_for_agent(TENANT_A, agent_id)
    matching = [e for e in events if e["event_type"] == "agent.deployed"]
    assert len(matching) == 1
    assert matching[0]["result"] == "success"
    assert matching[0]["principal_id"] == "ci-cd-webhook"
    assert matching[0]["resource"] == deploy["deployment_id"]


async def test_rollback_writes_version_rolled_back_security_audit_event(
    make_user_and_token,
) -> None:
    """Sprint 4 Phase 7 (S-13d) — agent.version_rolled_back was wired in
    Phase 2 (S-05) but never had a dedicated test confirming it actually
    lands in panasa-audit-log, unlike created/updated/deployed above."""
    _, token = await make_user_and_token(TENANT_A, role="developer")

    with TestClient(app) as client:
        app.state.git_provider = FakeGitProvider()
        created = client.post(
            "/api/v1/agents", json=_minimal_agent_payload(), headers=_bearer(token)
        ).json()
        agent_id = created["agent_id"]

        payload = _minimal_agent_payload()
        payload["configuration"]["temperature"] = 0.7
        client.put(
            f"/api/v1/agents/{agent_id}",
            json={"configuration": payload["configuration"], "change_description": "v2"},
            headers=_bearer(token),
        )

        client.post(
            f"/api/v1/agents/{agent_id}/rollback",
            json={"target_version": 1, "reason": "v2 regressed"},
            headers=_bearer(token),
        )

    events = _events_for_agent(TENANT_A, agent_id)
    matching = [e for e in events if e["event_type"] == "agent.version_rolled_back"]
    assert len(matching) == 1
    assert matching[0]["result"] == "success"
