"""POST /internal/deployment-complete — the real CI/CD's own "it worked"
signal (Generic Agent Runtime instruction, Part 6). Uses the real /deploy
flow (Phase 7) to get a genuine agent + deployment record to mark active,
same pattern as test_deploy_api.py."""

from __future__ import annotations

from typing import Any

from fastapi.testclient import TestClient

from app.config import settings
from app.main import app
from tests.fakes import FakeGitProvider

TENANT_A = "tenant-a"


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _minimal_agent_payload(name: str = "KYC Agent") -> dict[str, Any]:
    return {
        "name": name,
        "description": "Know Your Customer verification agent",
        "business_purpose": "Automate KYC document verification for onboarding",
        "agent_type": "standard",
        "configuration": {
            "model_id": "anthropic.claude-3-5-sonnet-20241022-v2:0",
            "model_provider": "bedrock",
            "system_prompt": "You are a KYC verification agent for {{company_name}}.",
        },
    }


async def _deploy_agent(client: TestClient, token: str) -> dict[str, Any]:
    app.state.git_provider = FakeGitProvider()
    created = client.post(
        "/api/v1/agents", json=_minimal_agent_payload(), headers=_bearer(token)
    ).json()
    agent_id = created["agent_id"]
    deploy = client.post(f"/api/v1/agents/{agent_id}/deploy", headers=_bearer(token)).json()
    return {"agent_id": agent_id, **deploy}


async def test_deployment_complete_marks_agent_active(monkeypatch, make_user_and_token) -> None:
    monkeypatch.setattr(settings, "internal_webhook_secret", "test-shared-secret")
    _, token = await make_user_and_token(TENANT_A, role="developer")

    with TestClient(app) as client:
        deploy = await _deploy_agent(client, token)

        response = client.post(
            "/api/v1/internal/deployment-complete",
            json={
                "agent_id": deploy["agent_id"],
                "tenant_id": TENANT_A,
                "deployment_id": deploy["deployment_id"],
                "version": deploy["version"],
                "status": "ACTIVE",
            },
            headers={"Authorization": "Bearer test-shared-secret"},
        )

        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "ACTIVE"
        assert body["live_version"] == deploy["version"]

        agent_detail = client.get(
            f"/api/v1/agents/{deploy['agent_id']}", headers=_bearer(token)
        ).json()
        assert agent_detail["agent"]["status"] == "ACTIVE"
        assert agent_detail["agent"]["live_version"] == deploy["version"]

        deployment_detail = client.get(
            f"/api/v1/deployments/{deploy['deployment_id']}", headers=_bearer(token)
        ).json()
        assert deployment_detail["status"] == "ACTIVE"
        assert deployment_detail["stages"]["HEALTH_CHECK"]["status"] == "PASSED"


async def test_deployment_complete_rejects_wrong_secret(monkeypatch, make_user_and_token) -> None:
    monkeypatch.setattr(settings, "internal_webhook_secret", "test-shared-secret")
    _, token = await make_user_and_token(TENANT_A, role="developer")

    with TestClient(app) as client:
        deploy = await _deploy_agent(client, token)

        response = client.post(
            "/api/v1/internal/deployment-complete",
            json={
                "agent_id": deploy["agent_id"],
                "tenant_id": TENANT_A,
                "deployment_id": deploy["deployment_id"],
                "version": deploy["version"],
                "status": "ACTIVE",
            },
            headers={"Authorization": "Bearer wrong-secret"},
        )

    assert response.status_code == 401


async def test_deployment_complete_disabled_when_secret_not_configured(
    monkeypatch, make_user_and_token
) -> None:
    monkeypatch.setattr(settings, "internal_webhook_secret", None)
    _, token = await make_user_and_token(TENANT_A, role="developer")

    with TestClient(app) as client:
        deploy = await _deploy_agent(client, token)

        response = client.post(
            "/api/v1/internal/deployment-complete",
            json={
                "agent_id": deploy["agent_id"],
                "tenant_id": TENANT_A,
                "deployment_id": deploy["deployment_id"],
                "version": deploy["version"],
                "status": "ACTIVE",
            },
            headers={"Authorization": "Bearer anything"},
        )

    assert response.status_code == 401


async def test_deployment_complete_404_for_unknown_agent(monkeypatch, make_user_and_token) -> None:
    monkeypatch.setattr(settings, "internal_webhook_secret", "test-shared-secret")
    _, token = await make_user_and_token(TENANT_A, role="developer")

    with TestClient(app) as client:
        deploy = await _deploy_agent(client, token)

        response = client.post(
            "/api/v1/internal/deployment-complete",
            json={
                "agent_id": "does-not-exist",
                "tenant_id": TENANT_A,
                "deployment_id": deploy["deployment_id"],
                "version": 1,
                "status": "ACTIVE",
            },
            headers={"Authorization": "Bearer test-shared-secret"},
        )

    assert response.status_code == 404
