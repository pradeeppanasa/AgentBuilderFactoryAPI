"""API tests for /api/v1/platform/skills (CLAUDE.md Section 38.3/38.11).

Admin-only write; read open to every role. Update creates a new version
snapshot in `version_history` without touching any agent's `skill_ids`.
Delete is blocked (409) while any agent still references the skill —
same reference-check pattern as the Knowledge Base / Guardrail Policy /
Bedrock Credentials libraries.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.main import app

TENANT_A = "tenant-a"
TENANT_B = "tenant-b"


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _config() -> dict:
    return {
        "model_id": "anthropic.claude-3-5-sonnet-20241022-v2:0",
        "model_provider": "bedrock",
        "system_prompt": "You are a helpful agent.",
    }


def _create_skill(client: TestClient, token: str, name: str = "Document Verification") -> dict:
    created = client.post(
        "/api/v1/platform/skills",
        json={
            "name": name,
            "description": "Verify submitted documents",
            "capability": "document_extraction",
            "prompt_fragment": "When verifying a document, extract...",
        },
        headers=_bearer(token),
    )
    assert created.status_code == 201
    return created.json()


async def test_admin_can_create_list_get_skill(make_user_and_token) -> None:
    _, admin_token = await make_user_and_token(TENANT_A, role="admin")

    with TestClient(app) as client:
        body = _create_skill(client, admin_token)
        assert body["name"] == "Document Verification"
        assert body["version"] == "1.0"
        assert body["status"] == "draft"
        assert body["version_history"] == []
        skill_id = body["skill_id"]

        fetched = client.get(
            f"/api/v1/platform/skills/{skill_id}", headers=_bearer(admin_token)
        )
        assert fetched.status_code == 200
        assert fetched.json()["skill_id"] == skill_id

        listed = client.get("/api/v1/platform/skills", headers=_bearer(admin_token))
        assert listed.status_code == 200
        assert [item["skill_id"] for item in listed.json()["items"]] == [skill_id]


async def test_get_unknown_skill_returns_404(make_user_and_token) -> None:
    _, admin_token = await make_user_and_token(TENANT_A, role="admin")

    with TestClient(app) as client:
        response = client.get(
            "/api/v1/platform/skills/does-not-exist", headers=_bearer(admin_token)
        )
        assert response.status_code == 404


async def test_skills_are_tenant_isolated(make_user_and_token) -> None:
    _, admin_a_token = await make_user_and_token(TENANT_A, role="admin")
    _, admin_b_token = await make_user_and_token(TENANT_B, role="admin")

    with TestClient(app) as client:
        body = _create_skill(client, admin_a_token)

        cross_tenant = client.get(
            f"/api/v1/platform/skills/{body['skill_id']}", headers=_bearer(admin_b_token)
        )
        assert cross_tenant.status_code == 404
        assert (
            client.get("/api/v1/platform/skills", headers=_bearer(admin_b_token)).json()["items"]
            == []
        )


async def test_non_admin_cannot_create_or_update_skill(make_user_and_token) -> None:
    _, admin_token = await make_user_and_token(TENANT_A, role="admin")
    _, dev_token = await make_user_and_token(TENANT_A, role="developer")

    with TestClient(app) as client:
        denied_create = client.post(
            "/api/v1/platform/skills",
            json={
                "name": "Risk Scoring",
                "description": "d",
                "capability": "risk_assessment",
                "prompt_fragment": "...",
            },
            headers=_bearer(dev_token),
        )
        assert denied_create.status_code == 403

        body = _create_skill(client, admin_token)
        denied_update = client.put(
            f"/api/v1/platform/skills/{body['skill_id']}",
            json={"change_description": "tweak", "name": "New name"},
            headers=_bearer(dev_token),
        )
        assert denied_update.status_code == 403

        # Read remains open to every role.
        allowed_read = client.get(
            f"/api/v1/platform/skills/{body['skill_id']}", headers=_bearer(dev_token)
        )
        assert allowed_read.status_code == 200


async def test_update_skill_creates_version_snapshot(make_user_and_token) -> None:
    _, admin_token = await make_user_and_token(TENANT_A, role="admin")

    with TestClient(app) as client:
        body = _create_skill(client, admin_token)
        skill_id = body["skill_id"]

        updated = client.put(
            f"/api/v1/platform/skills/{skill_id}",
            json={
                "change_description": "clarify extraction steps",
                "prompt_fragment": "When verifying a document, extract fields X, Y, Z...",
            },
            headers=_bearer(admin_token),
        )
        assert updated.status_code == 200
        updated_body = updated.json()
        assert updated_body["version"] == "1.1"
        assert updated_body["prompt_fragment"] == (
            "When verifying a document, extract fields X, Y, Z..."
        )
        # Untouched fields preserved.
        assert updated_body["name"] == body["name"]

        assert len(updated_body["version_history"]) == 1
        snapshot = updated_body["version_history"][0]
        assert snapshot["version"] == "1.0"
        assert snapshot["prompt_fragment"] == body["prompt_fragment"]
        assert snapshot["change_description"] == "clarify extraction steps"


async def test_status_only_update_does_not_bump_version(make_user_and_token) -> None:
    """Section 38.11: archive/restore/publish are lightweight status
    transitions, not content edits — they must not create a version bump
    or a version_history snapshot."""
    _, admin_token = await make_user_and_token(TENANT_A, role="admin")

    with TestClient(app) as client:
        body = _create_skill(client, admin_token)
        skill_id = body["skill_id"]

        archived = client.put(
            f"/api/v1/platform/skills/{skill_id}",
            json={"change_description": "archive", "status": "deprecated"},
            headers=_bearer(admin_token),
        )
        assert archived.status_code == 200
        archived_body = archived.json()
        assert archived_body["status"] == "deprecated"
        assert archived_body["version"] == "1.0"
        assert archived_body["version_history"] == []


async def test_update_unknown_skill_returns_404(make_user_and_token) -> None:
    _, admin_token = await make_user_and_token(TENANT_A, role="admin")

    with TestClient(app) as client:
        response = client.put(
            "/api/v1/platform/skills/does-not-exist",
            json={"change_description": "x", "name": "New name"},
            headers=_bearer(admin_token),
        )
        assert response.status_code == 404


async def test_delete_unreferenced_skill_succeeds(make_user_and_token) -> None:
    _, admin_token = await make_user_and_token(TENANT_A, role="admin")

    with TestClient(app) as client:
        body = _create_skill(client, admin_token)

        deleted = client.delete(
            f"/api/v1/platform/skills/{body['skill_id']}", headers=_bearer(admin_token)
        )
        assert deleted.status_code == 204
        assert (
            client.get(
                f"/api/v1/platform/skills/{body['skill_id']}", headers=_bearer(admin_token)
            ).status_code
            == 404
        )


async def test_delete_skill_blocked_while_referenced_by_agent(make_user_and_token) -> None:
    _, admin_token = await make_user_and_token(TENANT_A, role="admin")
    _, dev_token = await make_user_and_token(TENANT_A, role="developer")

    with TestClient(app) as client:
        skill_id = _create_skill(client, admin_token)["skill_id"]

        project_id = client.post(
            "/api/v1/projects",
            json={"name": "P", "description": "d"},
            headers=_bearer(dev_token),
        ).json()["project_id"]

        config = _config()
        config["skill_ids"] = [skill_id]
        client.post(
            f"/api/v1/projects/{project_id}/agents",
            json={
                "name": "Agent A",
                "description": "d",
                "business_purpose": "p",
                "agent_type": "task",
                "configuration": config,
            },
            headers=_bearer(dev_token),
        )

        denied = client.delete(
            f"/api/v1/platform/skills/{skill_id}", headers=_bearer(admin_token)
        )
        assert denied.status_code == 409
        body = denied.json()["detail"]
        assert body["referenced_by"][0]["type"] == "agent"
        assert body["referenced_by"][0]["project"] == project_id
