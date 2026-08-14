"""GET /deployments/{id} and GET /agents/{id}/deployments (CLAUDE.md Section 5.3).

Uses the real /deploy flow (Phase 7) to create a deployment, with
app.state.git_provider swapped for an in-memory fake so no real network
call happens — same pattern as test_deploy_api.py.
"""

from __future__ import annotations

from typing import Any

from fastapi.testclient import TestClient

from app.main import app
from app.modules.git_provider.base import GitProvider

TENANT_A = "tenant-a"
TENANT_B = "tenant-b"


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _minimal_agent_payload(name: str = "KYC Agent") -> dict[str, Any]:
    return {
        "name": name,
        "description": "Know Your Customer verification agent",
        "business_purpose": "Automate KYC document verification for onboarding",
        "agent_type": "task",
        "configuration": {
            "model_id": "anthropic.claude-3-5-sonnet-20241022-v2:0",
            "model_provider": "bedrock",
            "system_prompt": "You are a KYC verification agent for {{company_name}}.",
        },
    }


class FakeGitProvider(GitProvider):
    async def create_branch(self, repo: str, branch: str, from_branch: str = "main") -> None:
        pass

    async def commit_files(
        self, repo: str, branch: str, files: dict[str, str], message: str
    ) -> str:
        return "fake-commit-sha"

    async def create_pull_request(
        self, repo: str, branch: str, title: str, description: str
    ) -> str:
        return "99"

    async def merge_pull_request(self, repo: str, pr_id: str) -> None:
        pass

    async def close_pull_request(self, repo: str, pr_id: str, reason: str) -> None:
        pass


def _deploy(client: TestClient, token: str) -> dict[str, Any]:
    app.state.git_provider = FakeGitProvider()
    created = client.post(
        "/api/v1/agents", json=_minimal_agent_payload(), headers=_bearer(token)
    ).json()
    agent_id = created["agent_id"]
    deploy_response = client.post(f"/api/v1/agents/{agent_id}/deploy", headers=_bearer(token))
    body = deploy_response.json()
    body["agent_id_for_test"] = agent_id
    return body


async def test_get_deployment_returns_full_stage_detail(make_user_and_token) -> None:
    _, token = await make_user_and_token(TENANT_A, role="developer")

    with TestClient(app) as client:
        deployed = _deploy(client, token)
        response = client.get(
            f"/api/v1/deployments/{deployed['deployment_id']}", headers=_bearer(token)
        )

    assert response.status_code == 200
    body = response.json()
    assert body["agent_id"] == deployed["agent_id_for_test"]
    assert body["deployment_id"] == deployed["deployment_id"]
    assert body["version"] == 1
    assert body["status"] == "PENDING"
    assert body["iac_s3_key"]
    assert set(body["stages"]) == {
        "VALIDATING",
        "CHANGE_IMPACT",
        "GENERATING_IAC",
        "SECURITY_SCANNING",
        "EVALUATING",
        "TERRAFORM_VALIDATE",
        "TERRAFORM_PLAN",
        "POLICY_CHECK",
        "APPLYING",
        "DEPLOYING",
        "HEALTH_CHECK",
    }
    assert all(stage["status"] == "PENDING" for stage in body["stages"].values())


async def test_get_deployment_404_for_unknown_id(make_user_and_token) -> None:
    _, token = await make_user_and_token(TENANT_A, role="developer")

    with TestClient(app) as client:
        response = client.get("/api/v1/deployments/DEP-DOESNOTEXIST", headers=_bearer(token))

    assert response.status_code == 404


async def test_get_deployment_cross_tenant_returns_404(make_user_and_token) -> None:
    _, token_a = await make_user_and_token(TENANT_A, role="developer")
    _, token_b = await make_user_and_token(TENANT_B, role="developer")

    with TestClient(app) as client:
        deployed = _deploy(client, token_a)
        response = client.get(
            f"/api/v1/deployments/{deployed['deployment_id']}", headers=_bearer(token_b)
        )

    assert response.status_code == 404


async def test_list_agent_deployments(make_user_and_token) -> None:
    _, token = await make_user_and_token(TENANT_A, role="developer")

    with TestClient(app) as client:
        deployed = _deploy(client, token)
        agent_id = deployed["agent_id_for_test"]

        response = client.get(f"/api/v1/agents/{agent_id}/deployments", headers=_bearer(token))

    assert response.status_code == 200
    items = response.json()["items"]
    assert len(items) == 1
    assert items[0]["deployment_id"] == deployed["deployment_id"]


async def test_list_agent_deployments_cross_tenant_returns_404(make_user_and_token) -> None:
    _, token_a = await make_user_and_token(TENANT_A, role="developer")
    _, token_b = await make_user_and_token(TENANT_B, role="developer")

    with TestClient(app) as client:
        deployed = _deploy(client, token_a)
        agent_id = deployed["agent_id_for_test"]

        response = client.get(f"/api/v1/agents/{agent_id}/deployments", headers=_bearer(token_b))

    assert response.status_code == 404


async def test_auditor_can_read_deployment_status(make_user_and_token) -> None:
    _, developer_token = await make_user_and_token(TENANT_A, role="developer")
    _, auditor_token = await make_user_and_token(TENANT_A, role="auditor")

    with TestClient(app) as client:
        deployed = _deploy(client, developer_token)

        detail_response = client.get(
            f"/api/v1/deployments/{deployed['deployment_id']}", headers=_bearer(auditor_token)
        )
        list_response = client.get(
            f"/api/v1/agents/{deployed['agent_id_for_test']}/deployments",
            headers=_bearer(auditor_token),
        )

    assert detail_response.status_code == 200
    assert list_response.status_code == 200
