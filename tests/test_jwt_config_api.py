"""Sprint 4 Phase 4 — JWT/OAuth2 Enterprise Auth (S-12, CLAUDE.md Section
63.1).

Covers the Factory Runtime side only: the PUT .../credentials/jwt-config
endpoint's validation, record mutation, and role/tenant gating. The real
JWKS/RS256 verification logic lives entirely in the separate Generated
Agent Runtime (services/agent-runtime/auth.py's JwtAuthProvider, F8) and
is not exercised from here.
"""

from __future__ import annotations

from typing import Any

from fastapi.testclient import TestClient

from app.main import app

TENANT_A = "tenant-a"
TENANT_B = "tenant-b"

_JWKS_URL = "https://idp.example.com/.well-known/jwks.json"
_ISSUER = "https://idp.example.com/"


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _minimal_agent_payload(name: str = "JWT Config Test Agent") -> dict[str, Any]:
    return {
        "name": name,
        "description": "Agent used to exercise JWT config",
        "business_purpose": "Testing",
        "agent_type": "standard",
        "configuration": {
            "model_id": "anthropic.claude-3-5-sonnet-20241022-v2:0",
            "model_provider": "bedrock",
            "system_prompt": "You are a test agent.",
        },
    }


async def _create_agent(client: TestClient, token: str) -> str:
    created = client.post(
        "/api/v1/agents", json=_minimal_agent_payload(), headers=_bearer(token)
    ).json()
    agent_id: str = created["agent_id"]
    return agent_id


async def test_set_jwt_config_writes_all_fields(make_user_and_token) -> None:
    _, token = await make_user_and_token(TENANT_A, role="developer")

    with TestClient(app) as client:
        agent_id = await _create_agent(client, token)

        response = client.put(
            f"/api/v1/agents/{agent_id}/credentials/jwt-config",
            json={
                "jwt_issuer": _ISSUER,
                "jwt_audience": "panasa-agent-runtime",
                "jwt_jwks_url": _JWKS_URL,
                "jwt_tenant_claim": "org_id",
            },
            headers=_bearer(token),
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["agent_id"] == agent_id
        assert body["jwt_issuer"] == _ISSUER
        assert body["jwt_audience"] == "panasa-agent-runtime"
        assert body["jwt_jwks_url"] == _JWKS_URL
        assert body["jwt_tenant_claim"] == "org_id"

        detail = client.get(f"/api/v1/agents/{agent_id}", headers=_bearer(token)).json()["agent"]
        assert detail["jwt_issuer"] == _ISSUER
        assert detail["jwt_jwks_url"] == _JWKS_URL


async def test_new_agent_has_jwt_auth_off_by_default(make_user_and_token) -> None:
    _, token = await make_user_and_token(TENANT_A, role="developer")

    with TestClient(app) as client:
        agent_id = await _create_agent(client, token)

        detail = client.get(f"/api/v1/agents/{agent_id}", headers=_bearer(token)).json()["agent"]
        assert detail["jwt_issuer"] is None
        assert detail["jwt_jwks_url"] is None
        assert detail["jwt_tenant_claim"] == "tenant_id"


async def test_set_jwt_config_can_turn_jwt_auth_back_off(make_user_and_token) -> None:
    _, token = await make_user_and_token(TENANT_A, role="developer")

    with TestClient(app) as client:
        agent_id = await _create_agent(client, token)
        client.put(
            f"/api/v1/agents/{agent_id}/credentials/jwt-config",
            json={"jwt_issuer": _ISSUER, "jwt_jwks_url": _JWKS_URL},
            headers=_bearer(token),
        )

        response = client.put(
            f"/api/v1/agents/{agent_id}/credentials/jwt-config",
            json={},
            headers=_bearer(token),
        )
        assert response.status_code == 200, response.text
        assert response.json()["jwt_issuer"] is None
        assert response.json()["jwt_jwks_url"] is None


async def test_set_jwt_config_rejects_issuer_without_jwks_url(make_user_and_token) -> None:
    _, token = await make_user_and_token(TENANT_A, role="developer")

    with TestClient(app) as client:
        agent_id = await _create_agent(client, token)

        response = client.put(
            f"/api/v1/agents/{agent_id}/credentials/jwt-config",
            json={"jwt_issuer": _ISSUER},
            headers=_bearer(token),
        )
        assert response.status_code == 422


async def test_set_jwt_config_rejects_non_https_jwks_url(make_user_and_token) -> None:
    _, token = await make_user_and_token(TENANT_A, role="developer")

    with TestClient(app) as client:
        agent_id = await _create_agent(client, token)

        response = client.put(
            f"/api/v1/agents/{agent_id}/credentials/jwt-config",
            json={"jwt_issuer": _ISSUER, "jwt_jwks_url": "http://idp.example.com/jwks.json"},
            headers=_bearer(token),
        )
        assert response.status_code == 422


async def test_set_jwt_config_unknown_agent_is_404(make_user_and_token) -> None:
    _, token = await make_user_and_token(TENANT_A, role="developer")

    with TestClient(app) as client:
        response = client.put(
            "/api/v1/agents/does-not-exist/credentials/jwt-config",
            json={"jwt_issuer": _ISSUER, "jwt_jwks_url": _JWKS_URL},
            headers=_bearer(token),
        )
        assert response.status_code == 404


async def test_set_jwt_config_cross_tenant_returns_404(make_user_and_token) -> None:
    _, token_a = await make_user_and_token(TENANT_A, role="developer")
    _, token_b = await make_user_and_token(TENANT_B, role="developer")

    with TestClient(app) as client:
        agent_id = await _create_agent(client, token_a)

        response = client.put(
            f"/api/v1/agents/{agent_id}/credentials/jwt-config",
            json={"jwt_issuer": _ISSUER, "jwt_jwks_url": _JWKS_URL},
            headers=_bearer(token_b),
        )
        assert response.status_code == 404


async def test_set_jwt_config_forbidden_for_auditor_role(make_user_and_token) -> None:
    _, dev_token = await make_user_and_token(TENANT_A, role="developer")
    _, auditor_token = await make_user_and_token(TENANT_A, role="auditor")

    with TestClient(app) as client:
        agent_id = await _create_agent(client, dev_token)

        response = client.put(
            f"/api/v1/agents/{agent_id}/credentials/jwt-config",
            json={"jwt_issuer": _ISSUER, "jwt_jwks_url": _JWKS_URL},
            headers=_bearer(auditor_token),
        )
        assert response.status_code == 403
