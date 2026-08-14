"""API tests for /api/v1/platform/guardrail-policies (CLAUDE_Advanced_Config.md
Section 4.2 / 5 / 7, CLAUDE.md Section 37.10/37.11).

Reads open to every role; writes (create/update/delete) admin-only.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.main import app

TENANT_A = "tenant-a"
TENANT_B = "tenant-b"


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def test_admin_can_create_and_get_policy(make_user_and_token) -> None:
    _, admin_token = await make_user_and_token(TENANT_A, role="admin")

    with TestClient(app) as client:
        created = client.post(
            "/api/v1/platform/guardrail-policies",
            json={"name": "Strict", "description": "d"},
            headers=_bearer(admin_token),
        )
        assert created.status_code == 201
        policy_id = created.json()["policy_id"]

        fetched = client.get(
            f"/api/v1/platform/guardrail-policies/{policy_id}", headers=_bearer(admin_token)
        )
        assert fetched.status_code == 200
        assert fetched.json()["bert_block_threshold"] == 0.85


async def test_developer_cannot_create_policy(make_user_and_token) -> None:
    _, dev_token = await make_user_and_token(TENANT_A, role="developer")

    with TestClient(app) as client:
        response = client.post(
            "/api/v1/platform/guardrail-policies",
            json={"name": "X", "description": "d"},
            headers=_bearer(dev_token),
        )
    assert response.status_code == 403


async def test_developer_analyst_auditor_can_read_policies(make_user_and_token) -> None:
    _, admin_token = await make_user_and_token(TENANT_A, role="admin")
    _, analyst_token = await make_user_and_token(TENANT_A, role="analyst")
    _, auditor_token = await make_user_and_token(TENANT_A, role="auditor")

    with TestClient(app) as client:
        client.post(
            "/api/v1/platform/guardrail-policies",
            json={"name": "Readable", "description": "d"},
            headers=_bearer(admin_token),
        )

        for token in (analyst_token, auditor_token):
            listed = client.get("/api/v1/platform/guardrail-policies", headers=_bearer(token))
            assert listed.status_code == 200
            assert len(listed.json()["items"]) == 1


async def test_create_rejects_block_threshold_below_floor(make_user_and_token) -> None:
    _, admin_token = await make_user_and_token(TENANT_A, role="admin")

    with TestClient(app) as client:
        response = client.post(
            "/api/v1/platform/guardrail-policies",
            json={"name": "TooLoose", "description": "d", "bert_block_threshold": 0.5},
            headers=_bearer(admin_token),
        )
    assert response.status_code == 400


async def test_create_rejects_escalate_threshold_above_ceiling(make_user_and_token) -> None:
    _, admin_token = await make_user_and_token(TENANT_A, role="admin")

    with TestClient(app) as client:
        response = client.post(
            "/api/v1/platform/guardrail-policies",
            json={"name": "TooEager", "description": "d", "bert_escalate_threshold": 0.9},
            headers=_bearer(admin_token),
        )
    assert response.status_code == 400


async def test_update_policy_partial(make_user_and_token) -> None:
    _, admin_token = await make_user_and_token(TENANT_A, role="admin")

    with TestClient(app) as client:
        created = client.post(
            "/api/v1/platform/guardrail-policies",
            json={"name": "Editable", "description": "d"},
            headers=_bearer(admin_token),
        )
        policy_id = created.json()["policy_id"]

        updated = client.put(
            f"/api/v1/platform/guardrail-policies/{policy_id}",
            json={"description": "updated description"},
            headers=_bearer(admin_token),
        )
        assert updated.status_code == 200
        body = updated.json()
        assert body["description"] == "updated description"
        assert body["name"] == "Editable"


async def test_update_rejects_threshold_violation_using_merged_values(make_user_and_token) -> None:
    _, admin_token = await make_user_and_token(TENANT_A, role="admin")

    with TestClient(app) as client:
        created = client.post(
            "/api/v1/platform/guardrail-policies",
            json={"name": "Merge", "description": "d"},
            headers=_bearer(admin_token),
        )
        policy_id = created.json()["policy_id"]

        response = client.put(
            f"/api/v1/platform/guardrail-policies/{policy_id}",
            json={"bert_block_threshold": 0.6},
            headers=_bearer(admin_token),
        )
    assert response.status_code == 400


async def test_delete_blocked_when_referenced_by_agent(make_user_and_token) -> None:
    _, admin_token = await make_user_and_token(TENANT_A, role="admin")

    with TestClient(app) as client:
        policy = client.post(
            "/api/v1/platform/guardrail-policies",
            json={"name": "Referenced", "description": "d"},
            headers=_bearer(admin_token),
        )
        policy_id = policy.json()["policy_id"]

        agent = client.post(
            "/api/v1/agents",
            json={
                "name": "Guarded Agent",
                "description": "d",
                "business_purpose": "p",
                "agent_type": "task",
                "configuration": {
                    "model_id": "anthropic.claude-3-5-sonnet-20241022-v2:0",
                    "model_provider": "bedrock",
                    "system_prompt": "You help.",
                    "guardrail_policy_id": policy_id,
                },
            },
            headers=_bearer(admin_token),
        )
        assert agent.status_code == 201

        blocked = client.delete(
            f"/api/v1/platform/guardrail-policies/{policy_id}", headers=_bearer(admin_token)
        )
        assert blocked.status_code == 409


async def test_delete_succeeds_when_unreferenced(make_user_and_token) -> None:
    _, admin_token = await make_user_and_token(TENANT_A, role="admin")

    with TestClient(app) as client:
        policy = client.post(
            "/api/v1/platform/guardrail-policies",
            json={"name": "Unused", "description": "d"},
            headers=_bearer(admin_token),
        )
        policy_id = policy.json()["policy_id"]

        deleted = client.delete(
            f"/api/v1/platform/guardrail-policies/{policy_id}", headers=_bearer(admin_token)
        )
        assert deleted.status_code == 204


async def test_policy_is_tenant_scoped(make_user_and_token) -> None:
    _, admin_a = await make_user_and_token(TENANT_A, role="admin")
    _, admin_b = await make_user_and_token(TENANT_B, role="admin")

    with TestClient(app) as client:
        created = client.post(
            "/api/v1/platform/guardrail-policies",
            json={"name": "Tenant A Only", "description": "d"},
            headers=_bearer(admin_a),
        )
        policy_id = created.json()["policy_id"]

        hidden = client.get(
            f"/api/v1/platform/guardrail-policies/{policy_id}", headers=_bearer(admin_b)
        )
        assert hidden.status_code == 404
