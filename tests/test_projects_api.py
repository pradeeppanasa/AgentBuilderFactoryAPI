"""API tests for /api/v1/projects and /api/v1/projects/{id}/agents/...
(CLAUDE.md Section 38.2/38.6/38.7/38.11).

Project-scoped agents reuse the existing AgentRegistryStore entirely —
these tests focus on the project-id scoping and the draft/published/
deprecated/archived lifecycle layered on top; the underlying
create/read/update mechanics are already covered by test_agents_api.py.
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


# ── Projects ─────────────────────────────────────────────────────────────


async def test_create_list_get_update_project(make_user_and_token) -> None:
    _, dev_token = await make_user_and_token(TENANT_A, role="developer")

    with TestClient(app) as client:
        created = client.post(
            "/api/v1/projects",
            json={"name": "KYC Initiative", "description": "d"},
            headers=_bearer(dev_token),
        )
        assert created.status_code == 201
        project_id = created.json()["project_id"]

        fetched = client.get(f"/api/v1/projects/{project_id}", headers=_bearer(dev_token))
        assert fetched.status_code == 200
        assert fetched.json()["name"] == "KYC Initiative"

        listed = client.get("/api/v1/projects", headers=_bearer(dev_token))
        assert [p["project_id"] for p in listed.json()["items"]] == [project_id]

        updated = client.put(
            f"/api/v1/projects/{project_id}",
            json={"name": "KYC Initiative v2"},
            headers=_bearer(dev_token),
        )
        assert updated.status_code == 200
        assert updated.json()["name"] == "KYC Initiative v2"
        assert updated.json()["description"] == "d"  # untouched field preserved


async def test_get_unknown_project_returns_404(make_user_and_token) -> None:
    _, dev_token = await make_user_and_token(TENANT_A, role="developer")

    with TestClient(app) as client:
        response = client.get("/api/v1/projects/does-not-exist", headers=_bearer(dev_token))
        assert response.status_code == 404


async def test_projects_are_tenant_isolated(make_user_and_token) -> None:
    _, dev_a_token = await make_user_and_token(TENANT_A, role="developer")
    _, dev_b_token = await make_user_and_token(TENANT_B, role="developer")

    with TestClient(app) as client:
        created = client.post(
            "/api/v1/projects",
            json={"name": "Tenant A project", "description": "d"},
            headers=_bearer(dev_a_token),
        ).json()

        cross_tenant = client.get(
            f"/api/v1/projects/{created['project_id']}", headers=_bearer(dev_b_token)
        )
        assert cross_tenant.status_code == 404
        assert client.get("/api/v1/projects", headers=_bearer(dev_b_token)).json()["items"] == []


async def test_delete_project_blocked_while_it_has_agents(make_user_and_token) -> None:
    _, dev_token = await make_user_and_token(TENANT_A, role="developer")

    with TestClient(app) as client:
        project_id = client.post(
            "/api/v1/projects",
            json={"name": "Has agents", "description": "d"},
            headers=_bearer(dev_token),
        ).json()["project_id"]

        client.post(
            f"/api/v1/projects/{project_id}/agents",
            json={
                "name": "Agent A",
                "description": "d",
                "business_purpose": "p",
                "agent_type": "task",
                "configuration": _config(),
            },
            headers=_bearer(dev_token),
        )

        denied = client.delete(f"/api/v1/projects/{project_id}", headers=_bearer(dev_token))
        assert denied.status_code == 409
        body = denied.json()["detail"]
        assert body["referenced_by"][0]["type"] == "agent"
        assert body["referenced_by"][0]["project"] == project_id


async def test_delete_empty_project_succeeds(make_user_and_token) -> None:
    _, dev_token = await make_user_and_token(TENANT_A, role="developer")

    with TestClient(app) as client:
        project_id = client.post(
            "/api/v1/projects",
            json={"name": "Empty", "description": "d"},
            headers=_bearer(dev_token),
        ).json()["project_id"]

        deleted = client.delete(f"/api/v1/projects/{project_id}", headers=_bearer(dev_token))
        assert deleted.status_code == 204
        after = client.get(f"/api/v1/projects/{project_id}", headers=_bearer(dev_token))
        assert after.status_code == 404


# ── Project-scoped agents ────────────────────────────────────────────────


def _create_project_and_agent(client: TestClient, token: str) -> tuple[str, str]:
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
            "agent_type": "task",
            "configuration": _config(),
            "owner_email": "owner@example.com",
        },
        headers=_bearer(token),
    )
    assert created.status_code == 201
    return project_id, created.json()["agent_id"]


async def test_create_project_agent_starts_as_draft(make_user_and_token) -> None:
    _, dev_token = await make_user_and_token(TENANT_A, role="developer")

    with TestClient(app) as client:
        created_response = client.post(
            "/api/v1/projects",
            json={"name": "P", "description": "d"},
            headers=_bearer(dev_token),
        )
        project_id = created_response.json()["project_id"]

        created = client.post(
            f"/api/v1/projects/{project_id}/agents",
            json={
                "name": "Agent A",
                "description": "d",
                "business_purpose": "p",
                "agent_type": "task",
                "configuration": _config(),
            },
            headers=_bearer(dev_token),
        )
        assert created.status_code == 201
        body = created.json()
        assert body["project_lifecycle_status"] == "draft"
        assert body["version"] == 1

        agent = client.get(
            f"/api/v1/projects/{project_id}/agents/{body['agent_id']}", headers=_bearer(dev_token)
        )
        assert agent.status_code == 200
        assert agent.json()["project_id"] == project_id
        assert agent.json()["owner_email"] is None


async def test_create_agent_requires_existing_project(make_user_and_token) -> None:
    _, dev_token = await make_user_and_token(TENANT_A, role="developer")

    with TestClient(app) as client:
        response = client.post(
            "/api/v1/projects/does-not-exist/agents",
            json={
                "name": "Agent A",
                "description": "d",
                "business_purpose": "p",
                "agent_type": "task",
                "configuration": _config(),
            },
            headers=_bearer(dev_token),
        )
        assert response.status_code == 404


async def test_list_project_agents_scoped_to_project(make_user_and_token) -> None:
    _, dev_token = await make_user_and_token(TENANT_A, role="developer")

    with TestClient(app) as client:
        project_a, agent_a = _create_project_and_agent(client, dev_token)
        project_b, agent_b = _create_project_and_agent(client, dev_token)

        listed_a = client.get(f"/api/v1/projects/{project_a}/agents", headers=_bearer(dev_token))
        assert [a["agent_id"] for a in listed_a.json()["items"]] == [agent_a]

        listed_b = client.get(f"/api/v1/projects/{project_b}/agents", headers=_bearer(dev_token))
        assert [a["agent_id"] for a in listed_b.json()["items"]] == [agent_b]

        # An agent from project B is not visible under project A's path.
        cross = client.get(
            f"/api/v1/projects/{project_a}/agents/{agent_b}", headers=_bearer(dev_token)
        )
        assert cross.status_code == 404


async def test_publish_then_edit_moves_to_draft_again(make_user_and_token) -> None:
    _, dev_token = await make_user_and_token(TENANT_A, role="developer")

    with TestClient(app) as client:
        project_id, agent_id = _create_project_and_agent(client, dev_token)

        published = client.post(
            f"/api/v1/projects/{project_id}/agents/{agent_id}/publish", headers=_bearer(dev_token)
        )
        assert published.status_code == 200
        assert published.json()["project_lifecycle_status"] == "published"

        edited = client.put(
            f"/api/v1/projects/{project_id}/agents/{agent_id}",
            json={"configuration": _config(), "change_description": "tweak prompt"},
            headers=_bearer(dev_token),
        )
        assert edited.status_code == 200
        assert edited.json()["project_lifecycle_status"] == "draft"
        assert edited.json()["version"] == 2


async def test_republish_deprecates_previous_published_version(make_user_and_token) -> None:
    _, dev_token = await make_user_and_token(TENANT_A, role="developer")

    with TestClient(app) as client:
        project_id, agent_id = _create_project_and_agent(client, dev_token)
        client.post(
            f"/api/v1/projects/{project_id}/agents/{agent_id}/publish", headers=_bearer(dev_token)
        )
        client.put(
            f"/api/v1/projects/{project_id}/agents/{agent_id}",
            json={"configuration": _config(), "change_description": "v2"},
            headers=_bearer(dev_token),
        )
        client.post(
            f"/api/v1/projects/{project_id}/agents/{agent_id}/publish", headers=_bearer(dev_token)
        )

        v1 = client.get(f"/api/v1/agents/{agent_id}/versions/1", headers=_bearer(dev_token))
        v2 = client.get(f"/api/v1/agents/{agent_id}/versions/2", headers=_bearer(dev_token))
        assert v1.json()["project_lifecycle_status"] == "deprecated"
        assert v2.json()["project_lifecycle_status"] == "published"


async def test_rollback_flips_status_without_creating_new_version(make_user_and_token) -> None:
    _, dev_token = await make_user_and_token(TENANT_A, role="developer")

    with TestClient(app) as client:
        project_id, agent_id = _create_project_and_agent(client, dev_token)
        client.post(
            f"/api/v1/projects/{project_id}/agents/{agent_id}/publish", headers=_bearer(dev_token)
        )
        client.put(
            f"/api/v1/projects/{project_id}/agents/{agent_id}",
            json={"configuration": _config(), "change_description": "v2"},
            headers=_bearer(dev_token),
        )
        client.post(
            f"/api/v1/projects/{project_id}/agents/{agent_id}/publish", headers=_bearer(dev_token)
        )

        rolled_back = client.post(
            f"/api/v1/projects/{project_id}/agents/{agent_id}/rollback",
            json={"target_version": 1, "reason": "v2 regressed"},
            headers=_bearer(dev_token),
        )
        assert rolled_back.status_code == 200
        body = rolled_back.json()
        assert body["current_version"] == 1
        assert body["project_lifecycle_status"] == "published"

        # No new version was created — still exactly 2 versions exist.
        versions = client.get(f"/api/v1/agents/{agent_id}/versions", headers=_bearer(dev_token))
        assert len(versions.json()["items"]) == 2
        v1 = client.get(f"/api/v1/agents/{agent_id}/versions/1", headers=_bearer(dev_token))
        v2 = client.get(f"/api/v1/agents/{agent_id}/versions/2", headers=_bearer(dev_token))
        assert v1.json()["project_lifecycle_status"] == "published"
        assert v2.json()["project_lifecycle_status"] == "deprecated"


async def test_rollback_to_current_version_is_rejected(make_user_and_token) -> None:
    _, dev_token = await make_user_and_token(TENANT_A, role="developer")

    with TestClient(app) as client:
        project_id, agent_id = _create_project_and_agent(client, dev_token)

        response = client.post(
            f"/api/v1/projects/{project_id}/agents/{agent_id}/rollback",
            json={"target_version": 1, "reason": "no-op"},
            headers=_bearer(dev_token),
        )
        assert response.status_code == 400


async def test_delete_published_agent_requires_archive_first(make_user_and_token) -> None:
    _, dev_token = await make_user_and_token(TENANT_A, role="developer")

    with TestClient(app) as client:
        project_id, agent_id = _create_project_and_agent(client, dev_token)
        client.post(
            f"/api/v1/projects/{project_id}/agents/{agent_id}/publish", headers=_bearer(dev_token)
        )

        denied = client.delete(
            f"/api/v1/projects/{project_id}/agents/{agent_id}", headers=_bearer(dev_token)
        )
        assert denied.status_code == 422

        archived = client.post(
            f"/api/v1/projects/{project_id}/agents/{agent_id}/archive", headers=_bearer(dev_token)
        )
        assert archived.status_code == 200
        assert archived.json()["project_lifecycle_status"] == "archived"

        deleted = client.delete(
            f"/api/v1/projects/{project_id}/agents/{agent_id}", headers=_bearer(dev_token)
        )
        assert deleted.status_code == 204

        after = client.get(
            f"/api/v1/projects/{project_id}/agents/{agent_id}", headers=_bearer(dev_token)
        )
        assert after.status_code == 404


async def test_hard_delete_blocked_while_referenced_by_orchestrator(make_user_and_token) -> None:
    _, dev_token = await make_user_and_token(TENANT_A, role="developer")

    with TestClient(app) as client:
        project_id, sub_agent_id = _create_project_and_agent(client, dev_token)
        client.post(
            f"/api/v1/projects/{project_id}/agents/{sub_agent_id}/publish",
            headers=_bearer(dev_token),
        )
        client.post(
            f"/api/v1/projects/{project_id}/agents/{sub_agent_id}/archive",
            headers=_bearer(dev_token),
        )

        orchestrator_config = _config()
        orchestrator_config["orchestration"] = {
            "is_manager": True,
            "sub_agent_ids": [sub_agent_id],
        }
        client.post(
            f"/api/v1/projects/{project_id}/agents",
            json={
                "name": "Orchestrator",
                "description": "d",
                "business_purpose": "p",
                "agent_type": "orchestrator",
                "configuration": orchestrator_config,
            },
            headers=_bearer(dev_token),
        )

        denied = client.delete(
            f"/api/v1/projects/{project_id}/agents/{sub_agent_id}", headers=_bearer(dev_token)
        )
        assert denied.status_code == 409
        assert denied.json()["detail"]["referenced_by"][0]["type"] == "agent"


async def test_non_developer_cannot_write(make_user_and_token) -> None:
    _, dev_token = await make_user_and_token(TENANT_A, role="developer")
    _, auditor_token = await make_user_and_token(TENANT_A, role="auditor")

    with TestClient(app) as client:
        project_id, agent_id = _create_project_and_agent(client, dev_token)

        denied = client.post(
            f"/api/v1/projects/{project_id}/agents/{agent_id}/publish",
            headers=_bearer(auditor_token),
        )
        assert denied.status_code == 403

        allowed_read = client.get(
            f"/api/v1/projects/{project_id}/agents/{agent_id}", headers=_bearer(auditor_token)
        )
        assert allowed_read.status_code == 200
