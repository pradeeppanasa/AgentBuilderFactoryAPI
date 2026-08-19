"""POST /agents/{agent_id}/deploy — end-to-end through the real app, but with
app.state.git_provider swapped for an in-memory fake so the test never makes
a real call to GitHub. EventBridge still goes through moto (the bus is
created in conftest's mocked_aws fixture).
"""

from __future__ import annotations

from typing import Any

from fastapi.testclient import TestClient

from app.main import app
from tests.fakes import FakeGitProvider

TENANT_A = "tenant-a"
TENANT_B = "tenant-b"


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


async def test_deploy_happy_path_triggers_git_and_eventbridge(make_user_and_token) -> None:
    _, token = await make_user_and_token(TENANT_A, role="developer")

    with TestClient(app) as client:
        fake_git = FakeGitProvider()
        app.state.git_provider = fake_git

        created = client.post(
            "/api/v1/agents", json=_minimal_agent_payload(), headers=_bearer(token)
        ).json()
        agent_id = created["agent_id"]

        response = client.post(f"/api/v1/agents/{agent_id}/deploy", headers=_bearer(token))

        assert response.status_code == 200
        body = response.json()
        assert body["agent_id"] == agent_id
        assert body["version"] == 1
        assert body["deployment_id"].startswith("DEP-")
        assert body["status"] == "DEPLOYING"
        assert body["branch"] == f"panasa/agent-{agent_id}-v1-{body['deployment_id']}"
        assert body["pull_request_id"] == "99"

        detail = client.get(f"/api/v1/agents/{agent_id}", headers=_bearer(token)).json()
        version_detail = client.get(
            f"/api/v1/agents/{agent_id}/versions/1", headers=_bearer(token)
        ).json()

    assert detail["agent"]["status"] == "DEPLOYING"
    assert version_detail["deployment_id"] == body["deployment_id"]
    assert version_detail["iac_s3_key"]  # generate-iac ran as part of deploy

    assert len(fake_git.created_branches) == 1
    assert len(fake_git.committed_files) == 1
    committed_repo, committed_branch, committed_files, _message = fake_git.committed_files[0]
    assert any(path.startswith(f"terraform/agents/{agent_id}/") for path in committed_files)
    assert len(fake_git.opened_prs) == 1
    _repo, _branch, pr_title, pr_description = fake_git.opened_prs[0]
    assert agent_id in pr_title
    assert "Deploy" in pr_title
    assert "pending" in pr_description.lower()


async def test_deploy_404_for_unknown_agent(make_user_and_token) -> None:
    _, token = await make_user_and_token(TENANT_A, role="developer")

    with TestClient(app) as client:
        app.state.git_provider = FakeGitProvider()
        response = client.post("/api/v1/agents/does-not-exist/deploy", headers=_bearer(token))

    assert response.status_code == 404


async def test_deploy_forbidden_for_auditor(make_user_and_token) -> None:
    _, developer_token = await make_user_and_token(TENANT_A, role="developer")
    _, auditor_token = await make_user_and_token(TENANT_A, role="auditor")

    with TestClient(app) as client:
        app.state.git_provider = FakeGitProvider()
        created = client.post(
            "/api/v1/agents", json=_minimal_agent_payload(), headers=_bearer(developer_token)
        ).json()
        agent_id = created["agent_id"]

        response = client.post(f"/api/v1/agents/{agent_id}/deploy", headers=_bearer(auditor_token))

    assert response.status_code == 403


async def test_deploy_cross_tenant_returns_404(make_user_and_token) -> None:
    _, token_a = await make_user_and_token(TENANT_A, role="developer")
    _, token_b = await make_user_and_token(TENANT_B, role="developer")

    with TestClient(app) as client:
        app.state.git_provider = FakeGitProvider()
        created = client.post(
            "/api/v1/agents", json=_minimal_agent_payload(), headers=_bearer(token_a)
        ).json()
        agent_id = created["agent_id"]

        response = client.post(f"/api/v1/agents/{agent_id}/deploy", headers=_bearer(token_b))

    assert response.status_code == 404
