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


async def test_generate_iac_happy_path_persists_artifact(make_user_and_token) -> None:
    _, token = await make_user_and_token(TENANT_A, role="developer")

    with TestClient(app) as client:
        created = client.post(
            "/api/v1/agents", json=_minimal_agent_payload(), headers=_bearer(token)
        ).json()
        agent_id = created["agent_id"]

        response = client.post(f"/api/v1/agents/{agent_id}/generate-iac", headers=_bearer(token))

        assert response.status_code == 200
        body = response.json()
        assert body["agent_id"] == agent_id
        assert body["version"] == 1
        assert body["tool"] == "terraform"
        assert body["s3_key"]
        assert "base" in body["modules"]

        detail = client.get(f"/api/v1/agents/{agent_id}/versions/1", headers=_bearer(token)).json()

    assert detail["iac_s3_key"] == body["s3_key"]
    assert detail["iac_version"] == body["iac_version"]


async def test_generate_iac_404_for_unknown_agent(make_user_and_token) -> None:
    _, token = await make_user_and_token(TENANT_A, role="developer")

    with TestClient(app) as client:
        response = client.post("/api/v1/agents/does-not-exist/generate-iac", headers=_bearer(token))

    assert response.status_code == 404


async def test_generate_iac_forbidden_for_auditor(make_user_and_token) -> None:
    _, developer_token = await make_user_and_token(TENANT_A, role="developer")
    _, auditor_token = await make_user_and_token(TENANT_A, role="auditor")

    with TestClient(app) as client:
        created = client.post(
            "/api/v1/agents", json=_minimal_agent_payload(), headers=_bearer(developer_token)
        ).json()
        agent_id = created["agent_id"]

        response = client.post(
            f"/api/v1/agents/{agent_id}/generate-iac", headers=_bearer(auditor_token)
        )

    assert response.status_code == 403


async def test_generate_iac_cross_tenant_returns_404(make_user_and_token) -> None:
    _, token_a = await make_user_and_token(TENANT_A, role="developer")
    _, token_b = await make_user_and_token(TENANT_B, role="developer")

    with TestClient(app) as client:
        created = client.post(
            "/api/v1/agents", json=_minimal_agent_payload(), headers=_bearer(token_a)
        ).json()
        agent_id = created["agent_id"]

        response = client.post(f"/api/v1/agents/{agent_id}/generate-iac", headers=_bearer(token_b))

    assert response.status_code == 404
