"""API tests for /api/v1/hitl/reviews (CLAUDE.md Section 38.7/38.8).

Creating a review is open to every authenticated role (it represents an
in-flight agent invocation surfacing a decision). Listing/getting is also
open to every role. Deciding (approve/reject/request-info) is restricted
to developer (admin implicitly allowed), same as every other write
endpoint in this API.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.main import app

TENANT_A = "tenant-a"
TENANT_B = "tenant-b"


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _config(**overrides: object) -> dict:
    config = {
        "model_id": "anthropic.claude-3-5-sonnet-20241022-v2:0",
        "model_provider": "bedrock",
        "system_prompt": "You are a helpful agent.",
    }
    config.update(overrides)
    return config


def _create_project_and_agent(
    client: TestClient, token: str, config: dict | None = None
) -> tuple[str, str]:
    project_id = client.post(
        "/api/v1/projects",
        json={"name": "P", "description": "d"},
        headers=_bearer(token),
    ).json()["project_id"]
    created = client.post(
        f"/api/v1/projects/{project_id}/agents",
        json={
            "name": "Agent A",
            "description": "d",
            "business_purpose": "p",
            "agent_type": "standard",
            "configuration": config or _config(),
        },
        headers=_bearer(token),
    )
    assert created.status_code == 201
    return project_id, created.json()["agent_id"]


async def test_create_review_requires_existing_agent(make_user_and_token) -> None:
    _, dev_token = await make_user_and_token(TENANT_A, role="developer")

    with TestClient(app) as client:
        response = client.post(
            "/api/v1/hitl/reviews",
            json={
                "agent_id": "does-not-exist",
                "trigger_condition": "high_risk_decision",
                "context_summary": "Agent flagged a high-value transaction for review.",
            },
            headers=_bearer(dev_token),
        )
        assert response.status_code == 404


async def test_create_review_defaults_timeout_when_agent_has_no_hitl_config(
    make_user_and_token,
) -> None:
    _, dev_token = await make_user_and_token(TENANT_A, role="developer")

    with TestClient(app) as client:
        _project_id, agent_id = _create_project_and_agent(client, dev_token)

        created = client.post(
            "/api/v1/hitl/reviews",
            json={
                "agent_id": agent_id,
                "trigger_condition": "high_risk_decision",
                "context_summary": "Agent flagged a high-value transaction for review.",
            },
            headers=_bearer(dev_token),
        )
        assert created.status_code == 201
        body = created.json()
        assert body["status"] == "pending"
        assert body["timeout_hours"] == 24
        assert body["agent_id"] == agent_id
        assert body["reviewed_by"] is None


async def test_create_review_uses_agent_hitl_timeout(make_user_and_token) -> None:
    _, dev_token = await make_user_and_token(TENANT_A, role="developer")

    with TestClient(app) as client:
        config = _config(hitl={"enabled": True, "reviewer_emails": ["r@x.com"], "timeout_hours": 4})
        _project_id, agent_id = _create_project_and_agent(client, dev_token, config)

        created = client.post(
            "/api/v1/hitl/reviews",
            json={
                "agent_id": agent_id,
                "trigger_condition": "low_confidence",
                "context_summary": "Confidence below threshold.",
            },
            headers=_bearer(dev_token),
        )
        assert created.status_code == 201
        assert created.json()["timeout_hours"] == 4


async def test_get_unknown_review_returns_404(make_user_and_token) -> None:
    _, dev_token = await make_user_and_token(TENANT_A, role="developer")

    with TestClient(app) as client:
        response = client.get("/api/v1/hitl/reviews/does-not-exist", headers=_bearer(dev_token))
        assert response.status_code == 404


async def test_reviews_are_tenant_isolated(make_user_and_token) -> None:
    _, dev_a_token = await make_user_and_token(TENANT_A, role="developer")
    _, dev_b_token = await make_user_and_token(TENANT_B, role="developer")

    with TestClient(app) as client:
        _project_id, agent_id = _create_project_and_agent(client, dev_a_token)
        created = client.post(
            "/api/v1/hitl/reviews",
            json={
                "agent_id": agent_id,
                "trigger_condition": "always",
                "context_summary": "s",
            },
            headers=_bearer(dev_a_token),
        ).json()

        cross_tenant = client.get(
            f"/api/v1/hitl/reviews/{created['review_id']}", headers=_bearer(dev_b_token)
        )
        assert cross_tenant.status_code == 404
        assert (
            client.get("/api/v1/hitl/reviews", headers=_bearer(dev_b_token)).json()["items"] == []
        )


async def test_list_reviews_filter_by_status(make_user_and_token) -> None:
    _, dev_token = await make_user_and_token(TENANT_A, role="developer")

    with TestClient(app) as client:
        _project_id, agent_id = _create_project_and_agent(client, dev_token)
        review_id = client.post(
            "/api/v1/hitl/reviews",
            json={"agent_id": agent_id, "trigger_condition": "always", "context_summary": "s"},
            headers=_bearer(dev_token),
        ).json()["review_id"]

        client.post(
            f"/api/v1/hitl/reviews/{review_id}/approve",
            json={"decision_reason": "looks fine"},
            headers=_bearer(dev_token),
        )

        pending = client.get(
            "/api/v1/hitl/reviews", params={"review_status": "pending"}, headers=_bearer(dev_token)
        )
        assert pending.json()["items"] == []

        approved = client.get(
            "/api/v1/hitl/reviews",
            params={"review_status": "approved"},
            headers=_bearer(dev_token),
        )
        assert [r["review_id"] for r in approved.json()["items"]] == [review_id]


async def test_approve_review(make_user_and_token) -> None:
    _, dev_token = await make_user_and_token(TENANT_A, role="developer")

    with TestClient(app) as client:
        _project_id, agent_id = _create_project_and_agent(client, dev_token)
        review_id = client.post(
            "/api/v1/hitl/reviews",
            json={"agent_id": agent_id, "trigger_condition": "always", "context_summary": "s"},
            headers=_bearer(dev_token),
        ).json()["review_id"]

        approved = client.post(
            f"/api/v1/hitl/reviews/{review_id}/approve",
            json={"decision_reason": "looks fine"},
            headers=_bearer(dev_token),
        )
        assert approved.status_code == 200
        body = approved.json()
        assert body["status"] == "approved"
        assert body["decision_reason"] == "looks fine"
        assert body["reviewed_by"] is not None
        assert body["reviewed_at"] is not None


async def test_reject_review(make_user_and_token) -> None:
    _, dev_token = await make_user_and_token(TENANT_A, role="developer")

    with TestClient(app) as client:
        _project_id, agent_id = _create_project_and_agent(client, dev_token)
        review_id = client.post(
            "/api/v1/hitl/reviews",
            json={"agent_id": agent_id, "trigger_condition": "always", "context_summary": "s"},
            headers=_bearer(dev_token),
        ).json()["review_id"]

        rejected = client.post(
            f"/api/v1/hitl/reviews/{review_id}/reject",
            json={"decision_reason": "not compliant"},
            headers=_bearer(dev_token),
        )
        assert rejected.status_code == 200
        assert rejected.json()["status"] == "rejected"


async def test_request_info_then_decide(make_user_and_token) -> None:
    _, dev_token = await make_user_and_token(TENANT_A, role="developer")

    with TestClient(app) as client:
        _project_id, agent_id = _create_project_and_agent(client, dev_token)
        review_id = client.post(
            "/api/v1/hitl/reviews",
            json={"agent_id": agent_id, "trigger_condition": "always", "context_summary": "s"},
            headers=_bearer(dev_token),
        ).json()["review_id"]

        info_requested = client.post(
            f"/api/v1/hitl/reviews/{review_id}/request-info",
            json={"decision_reason": "need more context"},
            headers=_bearer(dev_token),
        )
        assert info_requested.status_code == 200
        assert info_requested.json()["status"] == "info_requested"

        # A decision no longer in "pending" cannot be decided again.
        denied = client.post(
            f"/api/v1/hitl/reviews/{review_id}/approve",
            json={},
            headers=_bearer(dev_token),
        )
        assert denied.status_code == 409


async def test_decide_unknown_review_returns_404(make_user_and_token) -> None:
    _, dev_token = await make_user_and_token(TENANT_A, role="developer")

    with TestClient(app) as client:
        response = client.post(
            "/api/v1/hitl/reviews/does-not-exist/approve",
            json={},
            headers=_bearer(dev_token),
        )
        assert response.status_code == 404


async def test_already_decided_review_cannot_be_decided_again(make_user_and_token) -> None:
    _, dev_token = await make_user_and_token(TENANT_A, role="developer")

    with TestClient(app) as client:
        _project_id, agent_id = _create_project_and_agent(client, dev_token)
        review_id = client.post(
            "/api/v1/hitl/reviews",
            json={"agent_id": agent_id, "trigger_condition": "always", "context_summary": "s"},
            headers=_bearer(dev_token),
        ).json()["review_id"]

        client.post(
            f"/api/v1/hitl/reviews/{review_id}/approve", json={}, headers=_bearer(dev_token)
        )
        second = client.post(
            f"/api/v1/hitl/reviews/{review_id}/reject", json={}, headers=_bearer(dev_token)
        )
        assert second.status_code == 409


async def test_non_developer_cannot_decide_but_can_read(make_user_and_token) -> None:
    _, dev_token = await make_user_and_token(TENANT_A, role="developer")
    _, auditor_token = await make_user_and_token(TENANT_A, role="auditor")

    with TestClient(app) as client:
        _project_id, agent_id = _create_project_and_agent(client, dev_token)
        review_id = client.post(
            "/api/v1/hitl/reviews",
            json={"agent_id": agent_id, "trigger_condition": "always", "context_summary": "s"},
            headers=_bearer(auditor_token),
        ).json()["review_id"]

        denied = client.post(
            f"/api/v1/hitl/reviews/{review_id}/approve",
            json={},
            headers=_bearer(auditor_token),
        )
        assert denied.status_code == 403

        allowed_read = client.get(
            f"/api/v1/hitl/reviews/{review_id}", headers=_bearer(auditor_token)
        )
        assert allowed_read.status_code == 200
