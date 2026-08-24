"""POST /agents/{agent_id}/deployments/{deployment_id}/approve (CLAUDE.md
Section 45.3/45.4, R50 — resolved as configurable, see
deployment/models.py's module docstring: `approval_mode` on each
DeploymentRecord, default "automated" preserving F1 exactly, opt-in
"manual" for R50/Stage 5's human-approval gate).

A "manual"-mode deployment reaching PENDING_APPROVAL is simulated by
writing directly to app.state.deployment_status_store — the same store the
real customer-side CI/CD writes to over the DynamoDB status table (R03/F2:
the Runtime itself never runs terraform or decides PASS/BLOCK, so there is
no in-process way to make a deployment organically reach PENDING_APPROVAL
other than reproducing what that external CI/CD would write).
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


async def _park_at_pending_approval(agent_id: str, deployment_id: str) -> None:
    """Stand in for the customer's CI/CD reaching POLICY_CHECK=PASS in
    "manual" mode and parking the deployment (Section 45.4) instead of
    auto-continuing to APPLYING."""
    await app.state.deployment_status_store.update_stage(
        agent_id,
        deployment_id,
        stage="POLICY_CHECK",
        stage_status="PASSED",
        overall_status="PENDING_APPROVAL",
    )


async def test_deploy_defaults_to_automated_approval_mode(make_user_and_token) -> None:
    _, token = await make_user_and_token(TENANT_A, role="developer")

    with TestClient(app) as client:
        deployed = _deploy(client, token)
        detail = client.get(
            f"/api/v1/deployments/{deployed['deployment_id']}", headers=_bearer(token)
        ).json()

    assert detail["approval_mode"] == "automated"
    assert detail["approved_by"] is None


async def test_approve_on_automated_mode_deployment_is_409(make_user_and_token) -> None:
    """F1/R06 unchanged by default: POLICY_CHECK already decided PASS/BLOCK
    on its own, so there is nothing for a human to approve here."""
    _, token = await make_user_and_token(TENANT_A, role="developer")

    with TestClient(app) as client:
        deployed = _deploy(client, token)

        response = client.post(
            f"/api/v1/agents/{deployed['agent_id_for_test']}/deployments/"
            f"{deployed['deployment_id']}/approve",
            headers=_bearer(token),
        )

    assert response.status_code == 409
    assert response.json()["detail"]["error"] == "approval_not_applicable"


async def test_manual_mode_deployment_starts_pending_and_is_approved(
    make_user_and_token,
) -> None:
    admin_user, admin_token = await make_user_and_token(TENANT_A, role="admin")
    dev_user, dev_token = await make_user_and_token(TENANT_A, role="developer", email=None)

    with TestClient(app) as client:
        saved = client.patch(
            "/api/v1/admin/settings/deployment",
            json={"default_approval_mode": "manual"},
            headers=_bearer(admin_token),
        )
        assert saved.status_code == 200

        deployed = _deploy(client, dev_token)
        agent_id = deployed["agent_id_for_test"]
        deployment_id = deployed["deployment_id"]

        detail = client.get(
            f"/api/v1/deployments/{deployment_id}", headers=_bearer(dev_token)
        ).json()
        assert detail["approval_mode"] == "manual"
        assert detail["status"] == "PENDING"

        # Not yet at PENDING_APPROVAL — approving now must be rejected.
        too_early = client.post(
            f"/api/v1/agents/{agent_id}/deployments/{deployment_id}/approve",
            headers=_bearer(dev_token),
        )
        assert too_early.status_code == 409
        assert too_early.json()["detail"]["error"] == "not_pending_approval"

        await _park_at_pending_approval(agent_id, deployment_id)

        approved = client.post(
            f"/api/v1/agents/{agent_id}/deployments/{deployment_id}/approve",
            headers=_bearer(dev_token),
        )

    assert approved.status_code == 200
    body = approved.json()
    assert body["approved_by"] == dev_user.email
    assert body["approved_at"] is not None
    assert body["status"] == "PENDING_APPROVAL"  # only the CI/CD moves it to APPLYING next

    _ = admin_user  # only used to obtain admin_token


async def test_approve_second_time_is_409(make_user_and_token) -> None:
    admin_user, admin_token = await make_user_and_token(TENANT_A, role="admin")
    _, dev_token = await make_user_and_token(TENANT_A, role="developer")

    with TestClient(app) as client:
        client.patch(
            "/api/v1/admin/settings/deployment",
            json={"default_approval_mode": "manual"},
            headers=_bearer(admin_token),
        )

        deployed = _deploy(client, dev_token)
        agent_id = deployed["agent_id_for_test"]
        deployment_id = deployed["deployment_id"]

        await _park_at_pending_approval(agent_id, deployment_id)

        first = client.post(
            f"/api/v1/agents/{agent_id}/deployments/{deployment_id}/approve",
            headers=_bearer(dev_token),
        )
        assert first.status_code == 200

        second = client.post(
            f"/api/v1/agents/{agent_id}/deployments/{deployment_id}/approve",
            headers=_bearer(dev_token),
        )

    assert second.status_code == 409
    assert second.json()["detail"]["error"] == "already_approved"

    _ = admin_user


async def test_approve_404_for_unknown_deployment(make_user_and_token) -> None:
    _, token = await make_user_and_token(TENANT_A, role="developer")

    with TestClient(app) as client:
        created = client.post(
            "/api/v1/agents", json=_minimal_agent_payload(), headers=_bearer(token)
        ).json()
        agent_id = created["agent_id"]

        response = client.post(
            f"/api/v1/agents/{agent_id}/deployments/DEP-DOESNOTEXIST/approve",
            headers=_bearer(token),
        )

    assert response.status_code == 404


async def test_approve_404_for_unknown_agent(make_user_and_token) -> None:
    _, token = await make_user_and_token(TENANT_A, role="developer")

    with TestClient(app) as client:
        response = client.post(
            "/api/v1/agents/does-not-exist/deployments/DEP-DOESNOTEXIST/approve",
            headers=_bearer(token),
        )

    assert response.status_code == 404


async def test_approve_forbidden_for_auditor(make_user_and_token) -> None:
    admin_user, admin_token = await make_user_and_token(TENANT_A, role="admin")
    _, dev_token = await make_user_and_token(TENANT_A, role="developer")
    _, auditor_token = await make_user_and_token(TENANT_A, role="auditor")

    with TestClient(app) as client:
        client.patch(
            "/api/v1/admin/settings/deployment",
            json={"default_approval_mode": "manual"},
            headers=_bearer(admin_token),
        )

        deployed = _deploy(client, dev_token)
        agent_id = deployed["agent_id_for_test"]
        deployment_id = deployed["deployment_id"]

        await _park_at_pending_approval(agent_id, deployment_id)

        response = client.post(
            f"/api/v1/agents/{agent_id}/deployments/{deployment_id}/approve",
            headers=_bearer(auditor_token),
        )

    assert response.status_code == 403

    _ = admin_user


async def test_approve_cross_tenant_returns_404(make_user_and_token) -> None:
    admin_a, admin_token_a = await make_user_and_token(TENANT_A, role="admin")
    _, dev_token_a = await make_user_and_token(TENANT_A, role="developer")
    _, dev_token_b = await make_user_and_token(TENANT_B, role="developer")

    with TestClient(app) as client:
        client.patch(
            "/api/v1/admin/settings/deployment",
            json={"default_approval_mode": "manual"},
            headers=_bearer(admin_token_a),
        )

        deployed = _deploy(client, dev_token_a)
        agent_id = deployed["agent_id_for_test"]
        deployment_id = deployed["deployment_id"]

        await _park_at_pending_approval(agent_id, deployment_id)

        response = client.post(
            f"/api/v1/agents/{agent_id}/deployments/{deployment_id}/approve",
            headers=_bearer(dev_token_b),
        )

    assert response.status_code == 404

    _ = admin_a
