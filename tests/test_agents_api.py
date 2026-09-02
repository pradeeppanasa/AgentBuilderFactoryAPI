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
        "agent_type": "standard",
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


async def test_create_agent_persists_tags_and_changelog(make_user_and_token) -> None:
    """QA U-20/U-21: CreateAgentRequest previously had no tags/changelog
    fields at all — create_agent() always wrote tags={} and hardcoded v1's
    change_description to "Initial version", silently dropping whatever the
    caller sent."""
    _, token = await make_user_and_token(TENANT_A, role="developer")

    with TestClient(app) as client:
        payload = _minimal_agent_payload()
        payload["tags"] = {"team": "kyc", "priority": "high"}
        payload["changelog"] = "Initial KYC agent for the onboarding pilot."
        created = client.post("/api/v1/agents", json=payload, headers=_bearer(token)).json()
        agent_id = created["agent_id"]

        agent_response = client.get(f"/api/v1/agents/{agent_id}", headers=_bearer(token))
        versions_response = client.get(
            f"/api/v1/agents/{agent_id}/versions", headers=_bearer(token)
        )

    assert agent_response.json()["agent"]["tags"] == {"team": "kyc", "priority": "high"}
    v1 = next(v for v in versions_response.json()["items"] if v["version"] == 1)
    assert v1["change_description"] == "Initial KYC agent for the onboarding pilot."


async def test_create_agent_without_tags_or_changelog_keeps_old_defaults(
    make_user_and_token,
) -> None:
    """Both fields are optional — omitting them must behave exactly as
    before this change (empty tags, "Initial version" changelog)."""
    _, token = await make_user_and_token(TENANT_A, role="developer")

    with TestClient(app) as client:
        created = client.post(
            "/api/v1/agents", json=_minimal_agent_payload(), headers=_bearer(token)
        ).json()
        agent_id = created["agent_id"]

        agent_response = client.get(f"/api/v1/agents/{agent_id}", headers=_bearer(token))
        versions_response = client.get(
            f"/api/v1/agents/{agent_id}/versions", headers=_bearer(token)
        )

    assert agent_response.json()["agent"]["tags"] == {}
    v1 = next(v for v in versions_response.json()["items"] if v["version"] == 1)
    assert v1["change_description"] == "Initial version"


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


# ── Clone / Fork ─────────────────────────────────────────────────────────


async def test_clone_agent_copies_config_as_new_draft_v1(make_user_and_token) -> None:
    _, token = await make_user_and_token(TENANT_A, role="developer")

    with TestClient(app) as client:
        payload = _minimal_agent_payload()
        payload["tags"] = {"team": "kyc"}
        source = client.post("/api/v1/agents", json=payload, headers=_bearer(token)).json()
        source_id = source["agent_id"]

        response = client.post(
            f"/api/v1/agents/{source_id}/clone",
            json={"name": "Copy of KYC Agent"},
            headers=_bearer(token),
        )
        assert response.status_code == 201
        body = response.json()
        clone_id = body["agent"]["agent_id"]

        # Editing the source AFTER cloning must never affect the already-
        # created clone — it's an independent agent, not a live reference.
        updated = client.put(
            f"/api/v1/agents/{source_id}",
            json={
                "change_description": "post-clone edit",
                "configuration": {
                    "model_id": "anthropic.claude-3-5-sonnet-20241022-v2:0",
                    "model_provider": "bedrock",
                    "system_prompt": "Edited after cloning — should NOT appear in the clone.",
                },
            },
            headers=_bearer(token),
        )
        assert updated.status_code == 200

        clone_after = client.get(f"/api/v1/agents/{clone_id}", headers=_bearer(token)).json()

    assert body["agent"]["name"] == "Copy of KYC Agent"
    assert clone_id != source_id
    assert body["agent"]["current_version"] == 1
    assert body["agent"]["live_version"] is None
    assert body["agent"]["status"] == "DRAFT"
    assert body["configuration"]["system_prompt"] == payload["configuration"]["system_prompt"]
    assert body["agent"]["description"] == payload["description"]
    assert body["agent"]["business_purpose"] == payload["business_purpose"]
    assert body["agent"]["tags"] == {"team": "kyc"}
    # Independence check: the clone's config is untouched by the source's
    # later edit.
    assert clone_after["configuration"]["system_prompt"] == payload["configuration"]["system_prompt"]


async def test_clone_agent_not_found_returns_404(make_user_and_token) -> None:
    _, token = await make_user_and_token(TENANT_A, role="developer")

    with TestClient(app) as client:
        response = client.post(
            "/api/v1/agents/does-not-exist/clone",
            json={"name": "Copy"},
            headers=_bearer(token),
        )

    assert response.status_code == 404


async def test_clone_agent_across_tenants_is_not_found(make_user_and_token) -> None:
    """R01 tenant isolation — cloning is scoped exactly like every other
    read: you can't clone an agent belonging to a different tenant."""
    _, token_a = await make_user_and_token(TENANT_A, role="developer")
    _, token_b = await make_user_and_token(TENANT_B, role="developer")

    with TestClient(app) as client:
        source = client.post(
            "/api/v1/agents", json=_minimal_agent_payload(), headers=_bearer(token_a)
        ).json()

        response = client.post(
            f"/api/v1/agents/{source['agent_id']}/clone",
            json={"name": "Copy"},
            headers=_bearer(token_b),
        )

    assert response.status_code == 404


async def test_auditor_cannot_clone_agent(make_user_and_token) -> None:
    _, dev_token = await make_user_and_token(TENANT_A, role="developer")
    _, auditor_token = await make_user_and_token(TENANT_A, role="auditor")

    with TestClient(app) as client:
        source = client.post(
            "/api/v1/agents", json=_minimal_agent_payload(), headers=_bearer(dev_token)
        ).json()

        response = client.post(
            f"/api/v1/agents/{source['agent_id']}/clone",
            json={"name": "Copy"},
            headers=_bearer(auditor_token),
        )

    assert response.status_code == 403


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
