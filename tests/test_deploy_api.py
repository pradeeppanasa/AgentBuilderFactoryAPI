"""POST /agents/{agent_id}/deploy — end-to-end through the real app, but with
app.state.git_provider swapped for an in-memory fake so the test never makes
a real call to GitHub. EventBridge still goes through moto (the bus is
created in conftest's mocked_aws fixture).
"""

from __future__ import annotations

from typing import Any

from fastapi.testclient import TestClient

from app.main import app
from tests.fakes import FailingGitProvider, FakeGitProvider

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


async def test_deploy_v1_creates_agent_repo_and_pushes_straight_to_main(
    make_user_and_token,
) -> None:
    """Section 45.2/45.3 — v1 (repo doesn't exist yet): create the
    per-agent repo, push straight to its default branch, no PR."""
    _, token = await make_user_and_token(TENANT_A, role="developer")

    with TestClient(app) as client:
        fake_git = FakeGitProvider()
        app.state.git_provider = fake_git

        created = client.post(
            "/api/v1/agents", json=_minimal_agent_payload(), headers=_bearer(token)
        ).json()
        agent_id = created["agent_id"]
        repo = f"test-org/panasa-iac-{agent_id}"

        response = client.post(f"/api/v1/agents/{agent_id}/deploy", headers=_bearer(token))

        assert response.status_code == 200
        body = response.json()
        assert body["agent_id"] == agent_id
        assert body["version"] == 1
        assert body["deployment_id"].startswith("DEP-")
        assert body["status"] == "DEPLOYING"
        assert body["branch"] == "main"
        assert body["pull_request_id"] is None

        detail = client.get(f"/api/v1/agents/{agent_id}", headers=_bearer(token)).json()
        version_detail = client.get(
            f"/api/v1/agents/{agent_id}/versions/1", headers=_bearer(token)
        ).json()
        deployment_detail = client.get(
            f"/api/v1/deployments/{body['deployment_id']}", headers=_bearer(token)
        ).json()

    assert detail["agent"]["status"] == "DEPLOYING"
    assert version_detail["deployment_id"] == body["deployment_id"]
    assert version_detail["iac_s3_key"]  # generate-iac ran as part of deploy
    assert deployment_detail["branch"] == "main"
    assert deployment_detail["pull_request_id"] is None

    assert fake_git.created_repos == [repo]
    assert fake_git.created_branches == []  # v1 never branches — pushes to main directly
    assert len(fake_git.committed_files) == 1
    committed_repo, committed_branch, committed_files, _message = fake_git.committed_files[0]
    assert committed_repo == repo
    assert committed_branch == "main"
    assert "README.md" in committed_files
    assert agent_id in committed_files["README.md"]
    # Section 45.6/R58 — default cicd_provider is github_actions; committed
    # once, alongside the repo's very first Terraform.
    assert ".github/workflows/panasa-deploy.yml" in committed_files


async def test_deploy_v1_commits_the_tenants_chosen_cicd_workflow_file(
    make_user_and_token,
) -> None:
    admin_user, admin_token = await make_user_and_token(TENANT_A, role="admin")
    _, dev_token = await make_user_and_token(TENANT_A, role="developer")

    with TestClient(app) as client:
        settings_saved = client.patch(
            "/api/v1/admin/settings/deployment",
            json={"default_approval_mode": "automated", "cicd_provider": "gitlab_ci"},
            headers=_bearer(admin_token),
        )
        assert settings_saved.status_code == 200

        fake_git = FakeGitProvider()
        app.state.git_provider = fake_git

        created = client.post(
            "/api/v1/agents", json=_minimal_agent_payload(), headers=_bearer(dev_token)
        ).json()
        agent_id = created["agent_id"]

        client.post(f"/api/v1/agents/{agent_id}/deploy", headers=_bearer(dev_token))

    _repo, _branch, committed_files, _message = fake_git.committed_files[0]
    assert ".gitlab-ci.yml" in committed_files
    assert ".github/workflows/panasa-deploy.yml" not in committed_files

    _ = admin_user
    assert fake_git.opened_prs == []


async def test_deploy_v2_plus_opens_pr_against_existing_repo(make_user_and_token) -> None:
    """Section 45.2/45.3 — repo already exists (v2+): branch + PR, never a
    direct push to the default branch."""
    _, token = await make_user_and_token(TENANT_A, role="developer")

    with TestClient(app) as client:
        created = client.post(
            "/api/v1/agents", json=_minimal_agent_payload(), headers=_bearer(token)
        ).json()
        agent_id = created["agent_id"]
        repo = f"test-org/panasa-iac-{agent_id}"

        fake_git = FakeGitProvider(existing_repos={repo})
        app.state.git_provider = fake_git

        response = client.post(f"/api/v1/agents/{agent_id}/deploy", headers=_bearer(token))

        assert response.status_code == 200
        body = response.json()
        assert body["branch"] == f"deploy/v1-{body['deployment_id']}"
        assert body["pull_request_id"] == "99"

    assert fake_git.created_repos == []  # already existed — never (re)created
    assert len(fake_git.created_branches) == 1
    created_repo, created_branch, from_branch = fake_git.created_branches[0]
    assert created_repo == repo
    assert created_branch == body["branch"]
    assert from_branch == "main"
    assert len(fake_git.opened_prs) == 1
    _repo, _branch, pr_title, pr_description = fake_git.opened_prs[0]
    assert agent_id in pr_title
    assert "Deploy" in pr_title
    assert "pending" in pr_description.lower()


async def test_deploy_git_provider_failure_returns_structured_502_not_bare_500(
    make_user_and_token,
) -> None:
    """TS02-A-03 — before this fix, an unhandled httpx.HTTPStatusError from
    GitHubProvider.create_branch (e.g. an invalid/expired git token)
    propagated straight out of the route as a bodyless 500. The UI must
    always get a structured, actionable error instead (Fix 3 of the TS02
    bug report), and the deployment record must be marked FAILED rather
    than left stuck at its initial PENDING stages."""
    _, token = await make_user_and_token(TENANT_A, role="developer")

    with TestClient(app) as client:
        app.state.git_provider = FailingGitProvider(status_code=401)

        created = client.post(
            "/api/v1/agents", json=_minimal_agent_payload(), headers=_bearer(token)
        ).json()
        agent_id = created["agent_id"]

        response = client.post(f"/api/v1/agents/{agent_id}/deploy", headers=_bearer(token))

    assert response.status_code == 502
    body = response.json()
    assert body["detail"]["error"] == "git_provider_failed"
    message = body["detail"]["message"].lower()
    assert "credentials" in message or "token" in message


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
