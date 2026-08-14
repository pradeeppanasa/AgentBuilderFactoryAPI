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
        "agent_type": "task",
        "configuration": {
            "model_id": "anthropic.claude-3-5-sonnet-20241022-v2:0",
            "model_provider": "bedrock",
            "system_prompt": "You are a KYC verification agent for {{company_name}}.",
        },
    }


async def test_list_versions_returns_newest_first(make_user_and_token) -> None:
    _, token = await make_user_and_token(TENANT_A, role="developer")

    with TestClient(app) as client:
        created = client.post(
            "/api/v1/agents", json=_minimal_agent_payload(), headers=_bearer(token)
        ).json()
        agent_id = created["agent_id"]

        update_payload = _minimal_agent_payload()["configuration"]
        update_payload["temperature"] = 0.9
        client.put(
            f"/api/v1/agents/{agent_id}",
            json={"configuration": update_payload, "change_description": "Bump temperature"},
            headers=_bearer(token),
        )

        response = client.get(f"/api/v1/agents/{agent_id}/versions", headers=_bearer(token))

    assert response.status_code == 200
    items = response.json()["items"]
    assert [item["version"] for item in items] == [2, 1]
    assert items[0]["change_description"] == "Bump temperature"
    assert items[1]["change_description"] == "Initial version"


async def test_get_version_detail_returns_full_configuration(make_user_and_token) -> None:
    _, token = await make_user_and_token(TENANT_A, role="developer")

    with TestClient(app) as client:
        created = client.post(
            "/api/v1/agents", json=_minimal_agent_payload(), headers=_bearer(token)
        ).json()
        agent_id = created["agent_id"]

        response = client.get(f"/api/v1/agents/{agent_id}/versions/1", headers=_bearer(token))

    assert response.status_code == 200
    body = response.json()
    assert body["version"] == 1
    assert body["configuration"]["model_id"] == "anthropic.claude-3-5-sonnet-20241022-v2:0"
    assert body["capability_contract"]["agent_id"] == agent_id


async def test_get_version_detail_404_for_unknown_version(make_user_and_token) -> None:
    _, token = await make_user_and_token(TENANT_A, role="developer")

    with TestClient(app) as client:
        created = client.post(
            "/api/v1/agents", json=_minimal_agent_payload(), headers=_bearer(token)
        ).json()
        agent_id = created["agent_id"]

        response = client.get(f"/api/v1/agents/{agent_id}/versions/99", headers=_bearer(token))

    assert response.status_code == 404


async def test_diff_of_initial_version_shows_everything_added(make_user_and_token) -> None:
    _, token = await make_user_and_token(TENANT_A, role="developer")

    with TestClient(app) as client:
        created = client.post(
            "/api/v1/agents", json=_minimal_agent_payload(), headers=_bearer(token)
        ).json()
        agent_id = created["agent_id"]

        response = client.get(f"/api/v1/agents/{agent_id}/versions/1/diff", headers=_bearer(token))

    assert response.status_code == 200
    body = response.json()
    assert body["from_version"] is None
    assert body["to_version"] == 1
    assert body["config_diff"]["changed"] == []
    assert body["config_diff"]["removed"] == []
    added_fields = {entry["field"] for entry in body["config_diff"]["added"]}
    assert "model_id" in added_fields


async def test_diff_between_versions_reports_changed_and_added_fields(make_user_and_token) -> None:
    _, token = await make_user_and_token(TENANT_A, role="developer")

    with TestClient(app) as client:
        created = client.post(
            "/api/v1/agents", json=_minimal_agent_payload(), headers=_bearer(token)
        ).json()
        agent_id = created["agent_id"]

        update_payload = _minimal_agent_payload()["configuration"]
        update_payload["temperature"] = 0.9
        update_payload["tools"] = [
            {
                "tool_id": "jira",
                "tool_name": "Jira",
                "executor_type": "http",
                "input_schema": {},
            }
        ]
        client.put(
            f"/api/v1/agents/{agent_id}",
            json={"configuration": update_payload, "change_description": "Add Jira tool"},
            headers=_bearer(token),
        )

        response = client.get(f"/api/v1/agents/{agent_id}/versions/2/diff", headers=_bearer(token))

    assert response.status_code == 200
    body = response.json()
    assert body["from_version"] == 1
    assert body["to_version"] == 2

    changed_fields = {entry["field"]: entry for entry in body["config_diff"]["changed"]}
    assert changed_fields["temperature"]["from"] == 0.3
    assert changed_fields["temperature"]["to"] == 0.9

    added_fields = {entry["field"] for entry in body["config_diff"]["added"]}
    assert "tools[0]" in added_fields


async def test_rollback_creates_new_version_from_target_config(make_user_and_token) -> None:
    _, token = await make_user_and_token(TENANT_A, role="developer")

    with TestClient(app) as client:
        fake_git = FakeGitProvider()
        app.state.git_provider = fake_git

        created = client.post(
            "/api/v1/agents", json=_minimal_agent_payload(), headers=_bearer(token)
        ).json()
        agent_id = created["agent_id"]

        v2_config = _minimal_agent_payload()["configuration"]
        v2_config["temperature"] = 0.9
        client.put(
            f"/api/v1/agents/{agent_id}",
            json={"configuration": v2_config, "change_description": "v2"},
            headers=_bearer(token),
        )

        v3_config = _minimal_agent_payload()["configuration"]
        v3_config["temperature"] = 0.1
        client.put(
            f"/api/v1/agents/{agent_id}",
            json={"configuration": v3_config, "change_description": "v3"},
            headers=_bearer(token),
        )

        response = client.post(
            f"/api/v1/agents/{agent_id}/rollback",
            json={"target_version": 1, "reason": "v3 causing latency spike"},
            headers=_bearer(token),
        )

    assert response.status_code == 200
    body = response.json()
    assert body["version"] == 4
    assert body["rolled_back_from_version"] == 3
    # Phase 13: rollback now triggers a real deployment, same as POST /deploy.
    assert body["status"] == "DEPLOYING"
    assert body["deployment_id"].startswith("DEP-")
    assert body["pull_request_id"] == "99"
    assert len(fake_git.opened_prs) == 1

    with TestClient(app) as client:
        detail = client.get(f"/api/v1/agents/{agent_id}", headers=_bearer(token)).json()
        version_detail = client.get(
            f"/api/v1/agents/{agent_id}/versions/4", headers=_bearer(token)
        ).json()

    assert detail["agent"]["current_version"] == 4
    assert detail["configuration"]["temperature"] == 0.3  # back to v1's value
    assert version_detail["deployment_id"] == body["deployment_id"]
    assert version_detail["iac_s3_key"]


async def test_rollback_to_current_version_returns_400(make_user_and_token) -> None:
    _, token = await make_user_and_token(TENANT_A, role="developer")

    with TestClient(app) as client:
        created = client.post(
            "/api/v1/agents", json=_minimal_agent_payload(), headers=_bearer(token)
        ).json()
        agent_id = created["agent_id"]

        response = client.post(
            f"/api/v1/agents/{agent_id}/rollback",
            json={"target_version": 1, "reason": "no-op"},
            headers=_bearer(token),
        )

    assert response.status_code == 400


async def test_rollback_to_unknown_version_returns_404(make_user_and_token) -> None:
    _, token = await make_user_and_token(TENANT_A, role="developer")

    with TestClient(app) as client:
        created = client.post(
            "/api/v1/agents", json=_minimal_agent_payload(), headers=_bearer(token)
        ).json()
        agent_id = created["agent_id"]

        response = client.post(
            f"/api/v1/agents/{agent_id}/rollback",
            json={"target_version": 99, "reason": "does not exist"},
            headers=_bearer(token),
        )

    assert response.status_code == 404


async def test_auditor_cannot_rollback(make_user_and_token) -> None:
    _, developer_token = await make_user_and_token(TENANT_A, role="developer")
    _, auditor_token = await make_user_and_token(TENANT_A, role="auditor")

    with TestClient(app) as client:
        created = client.post(
            "/api/v1/agents", json=_minimal_agent_payload(), headers=_bearer(developer_token)
        ).json()
        agent_id = created["agent_id"]

        update_payload = _minimal_agent_payload()["configuration"]
        update_payload["temperature"] = 0.9
        client.put(
            f"/api/v1/agents/{agent_id}",
            json={"configuration": update_payload, "change_description": "v2"},
            headers=_bearer(developer_token),
        )

        response = client.post(
            f"/api/v1/agents/{agent_id}/rollback",
            json={"target_version": 1, "reason": "attempt"},
            headers=_bearer(auditor_token),
        )

    assert response.status_code == 403


async def test_cross_tenant_cannot_access_versions(make_user_and_token) -> None:
    _, token_a = await make_user_and_token(TENANT_A, role="developer")
    _, token_b = await make_user_and_token(TENANT_B, role="developer")

    with TestClient(app) as client:
        created = client.post(
            "/api/v1/agents", json=_minimal_agent_payload(), headers=_bearer(token_a)
        ).json()
        agent_id = created["agent_id"]

        list_response = client.get(f"/api/v1/agents/{agent_id}/versions", headers=_bearer(token_b))
        detail_response = client.get(
            f"/api/v1/agents/{agent_id}/versions/1", headers=_bearer(token_b)
        )
        diff_response = client.get(
            f"/api/v1/agents/{agent_id}/versions/1/diff", headers=_bearer(token_b)
        )
        rollback_response = client.post(
            f"/api/v1/agents/{agent_id}/rollback",
            json={"target_version": 1, "reason": "cross-tenant attempt"},
            headers=_bearer(token_b),
        )

    assert list_response.status_code == 404
    assert detail_response.status_code == 404
    assert diff_response.status_code == 404
    assert rollback_response.status_code == 404
