from typing import Any

from fastapi.testclient import TestClient

from app.main import app

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


async def test_create_agent_returns_201_with_v1_draft(make_user_and_token) -> None:
    _, token = await make_user_and_token(TENANT_A, role="developer")

    with TestClient(app) as client:
        response = client.post(
            "/api/v1/agents", json=_minimal_agent_payload(), headers=_bearer(token)
        )

    assert response.status_code == 201
    body = response.json()
    assert body["version"] == 1
    assert body["status"] == "DRAFT"
    assert body["agent_id"].startswith("kyc-agent-")


async def test_get_agent_returns_current_configuration(make_user_and_token) -> None:
    _, token = await make_user_and_token(TENANT_A, role="developer")

    with TestClient(app) as client:
        created = client.post(
            "/api/v1/agents", json=_minimal_agent_payload(), headers=_bearer(token)
        ).json()

        response = client.get(f"/api/v1/agents/{created['agent_id']}", headers=_bearer(token))

    assert response.status_code == 200
    body = response.json()
    assert body["agent"]["agent_id"] == created["agent_id"]
    assert body["agent"]["current_version"] == 1
    assert body["configuration"]["model_id"] == "anthropic.claude-3-5-sonnet-20241022-v2:0"
    # R11 — capability contract must always exist alongside the configuration
    assert body["capability_contract"]["agent_id"] == created["agent_id"]


async def test_update_agent_creates_new_version_without_overwriting_v1(make_user_and_token) -> None:
    _, token = await make_user_and_token(TENANT_A, role="developer")

    with TestClient(app) as client:
        created = client.post(
            "/api/v1/agents", json=_minimal_agent_payload(), headers=_bearer(token)
        ).json()
        agent_id = created["agent_id"]

        update_payload = _minimal_agent_payload()["configuration"]
        update_payload["temperature"] = 0.9
        response = client.put(
            f"/api/v1/agents/{agent_id}",
            json={"configuration": update_payload, "change_description": "Bump temperature"},
            headers=_bearer(token),
        )

        assert response.status_code == 200
        body = response.json()
        assert body["version"] == 2

        detail = client.get(f"/api/v1/agents/{agent_id}", headers=_bearer(token)).json()
        assert detail["agent"]["current_version"] == 2
        assert detail["configuration"]["temperature"] == 0.9


async def test_delete_agent_sets_status_deprecated(make_user_and_token) -> None:
    _, token = await make_user_and_token(TENANT_A, role="developer")

    with TestClient(app) as client:
        created = client.post(
            "/api/v1/agents", json=_minimal_agent_payload(), headers=_bearer(token)
        ).json()
        agent_id = created["agent_id"]

        response = client.delete(f"/api/v1/agents/{agent_id}", headers=_bearer(token))

    assert response.status_code == 200
    assert response.json()["status"] == "DEPRECATED"


async def test_list_agents_filters_by_status(make_user_and_token) -> None:
    _, token = await make_user_and_token(TENANT_A, role="developer")

    with TestClient(app) as client:
        client.post(
            "/api/v1/agents", json=_minimal_agent_payload("Agent One"), headers=_bearer(token)
        )
        created_two = client.post(
            "/api/v1/agents", json=_minimal_agent_payload("Agent Two"), headers=_bearer(token)
        ).json()
        client.delete(f"/api/v1/agents/{created_two['agent_id']}", headers=_bearer(token))

        active = client.get(
            "/api/v1/agents", params={"status": "DRAFT"}, headers=_bearer(token)
        ).json()
        deprecated = client.get(
            "/api/v1/agents", params={"status": "DEPRECATED"}, headers=_bearer(token)
        ).json()

    assert len(active["items"]) == 1
    assert active["items"][0]["name"] == "Agent One"
    assert len(deprecated["items"]) == 1
    assert deprecated["items"][0]["name"] == "Agent Two"


def _orchestrator_payload(
    name: str, sub_agents: list[dict[str, Any]] | None = None
) -> dict[str, Any]:
    payload = _minimal_agent_payload(name)
    payload["agent_type"] = "orchestrator"
    payload["configuration"]["orchestration"] = {
        "is_manager": True,
        "sub_agents": sub_agents or [],
    }
    return payload


async def test_orchestrator_circular_dependency_is_rejected(make_user_and_token) -> None:
    _, token = await make_user_and_token(TENANT_A, role="developer")

    with TestClient(app) as client:
        agent_a = client.post(
            "/api/v1/agents", json=_orchestrator_payload("Orchestrator A"), headers=_bearer(token)
        ).json()
        agent_a_id = agent_a["agent_id"]

        b_payload = _orchestrator_payload(
            "Orchestrator B",
            sub_agents=[
                {
                    "agent_id": agent_a_id,
                    "agent_name": "Orchestrator A",
                    "capability_description": "Handles KYC checks",
                }
            ],
        )
        agent_b = client.post("/api/v1/agents", json=b_payload, headers=_bearer(token)).json()
        agent_b_id = agent_b["agent_id"]

        # A -> B already exists via B's sub_agents; now try B -> A -> B by pointing A at B.
        cyclic_config = _orchestrator_payload(
            "Orchestrator A",
            sub_agents=[
                {
                    "agent_id": agent_b_id,
                    "agent_name": "Orchestrator B",
                    "capability_description": "Handles routing",
                }
            ],
        )["configuration"]
        response = client.put(
            f"/api/v1/agents/{agent_a_id}",
            json={"configuration": cyclic_config, "change_description": "attempt cycle"},
            headers=_bearer(token),
        )

    assert response.status_code == 409


async def test_agent_not_found_returns_404(make_user_and_token) -> None:
    _, token = await make_user_and_token(TENANT_A, role="developer")

    with TestClient(app) as client:
        response = client.get("/api/v1/agents/does-not-exist", headers=_bearer(token))

    assert response.status_code == 404


# ── RBAC ─────────────────────────────────────────────────────────────────


async def test_auditor_cannot_create_agent(make_user_and_token) -> None:
    _, token = await make_user_and_token(TENANT_A, role="auditor")

    with TestClient(app) as client:
        response = client.post(
            "/api/v1/agents", json=_minimal_agent_payload(), headers=_bearer(token)
        )

    assert response.status_code == 403


async def test_auditor_can_list_agents(make_user_and_token) -> None:
    developer, developer_token = await make_user_and_token(TENANT_A, role="developer")
    _, auditor_token = await make_user_and_token(TENANT_A, role="auditor")

    with TestClient(app) as client:
        client.post(
            "/api/v1/agents", json=_minimal_agent_payload(), headers=_bearer(developer_token)
        )
        response = client.get("/api/v1/agents", headers=_bearer(auditor_token))

    assert response.status_code == 200
    assert len(response.json()["items"]) == 1


async def test_missing_bearer_token_returns_403() -> None:
    # FastAPI's HTTPBearer raises 403 (not 401) when no Authorization header is presented at all.
    with TestClient(app) as client:
        response = client.get("/api/v1/agents")

    assert response.status_code == 403


# ── Tenant isolation — the cross-tenant leak test MUST fail to leak ─────────


async def test_cross_tenant_get_does_not_leak_agent(make_user_and_token) -> None:
    _, token_a = await make_user_and_token(TENANT_A, role="developer")
    _, token_b = await make_user_and_token(TENANT_B, role="developer")

    with TestClient(app) as client:
        created = client.post(
            "/api/v1/agents", json=_minimal_agent_payload(), headers=_bearer(token_a)
        ).json()

        leaked = client.get(f"/api/v1/agents/{created['agent_id']}", headers=_bearer(token_b))

    assert leaked.status_code == 404


async def test_cross_tenant_list_does_not_leak_agents(make_user_and_token) -> None:
    _, token_a = await make_user_and_token(TENANT_A, role="developer")
    _, token_b = await make_user_and_token(TENANT_B, role="developer")

    with TestClient(app) as client:
        client.post("/api/v1/agents", json=_minimal_agent_payload(), headers=_bearer(token_a))

        tenant_b_list = client.get("/api/v1/agents", headers=_bearer(token_b)).json()

    assert tenant_b_list["items"] == []


async def test_cross_tenant_update_does_not_leak_agent(make_user_and_token) -> None:
    _, token_a = await make_user_and_token(TENANT_A, role="developer")
    _, token_b = await make_user_and_token(TENANT_B, role="developer")

    with TestClient(app) as client:
        created = client.post(
            "/api/v1/agents", json=_minimal_agent_payload(), headers=_bearer(token_a)
        ).json()

        update_payload = _minimal_agent_payload()["configuration"]
        response = client.put(
            f"/api/v1/agents/{created['agent_id']}",
            json={
                "configuration": update_payload,
                "change_description": "Attempted cross-tenant edit",
            },
            headers=_bearer(token_b),
        )

    assert response.status_code == 404


async def test_cross_tenant_delete_does_not_leak_agent(make_user_and_token) -> None:
    _, token_a = await make_user_and_token(TENANT_A, role="developer")
    _, token_b = await make_user_and_token(TENANT_B, role="developer")

    with TestClient(app) as client:
        created = client.post(
            "/api/v1/agents", json=_minimal_agent_payload(), headers=_bearer(token_a)
        ).json()

        response = client.delete(f"/api/v1/agents/{created['agent_id']}", headers=_bearer(token_b))

    assert response.status_code == 404
