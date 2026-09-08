"""Sprint 3 Phase 8 — API Key Rotation (S-02, CLAUDE.md Section 64.4, R60-R63).

Covers the Factory Runtime side only: initial provisioning at create_agent
time, the rotate/revoke endpoints' record mutations, audit events, and
mode/role/tenant gating. The dual-key *validation* logic itself (accepting
either the current or a still-in-grace previous key) lives entirely in the
separate Generated Agent Runtime (services/agent-runtime/auth.py, Phase 1,
F8) and is not exercised from here.
"""

from __future__ import annotations

import time
from typing import Any

from fastapi.testclient import TestClient

from app.config import settings
from app.main import app

TENANT_A = "tenant-a"
TENANT_B = "tenant-b"


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _minimal_agent_payload(name: str = "Rotation Test Agent") -> dict[str, Any]:
    return {
        "name": name,
        "description": "Agent used to exercise API key rotation",
        "business_purpose": "Testing",
        "agent_type": "standard",
        "configuration": {
            "model_id": "anthropic.claude-3-5-sonnet-20241022-v2:0",
            "model_provider": "bedrock",
            "system_prompt": "You are a test agent.",
        },
    }


async def test_create_agent_in_prototype_mode_provisions_api_key(make_user_and_token) -> None:
    _, token = await make_user_and_token(TENANT_A, role="developer")

    with TestClient(app) as client:
        response = client.post(
            "/api/v1/agents", json=_minimal_agent_payload(), headers=_bearer(token)
        )
        assert response.status_code == 201, response.text
        body = response.json()
        assert body["api_key"] is not None
        assert body["api_key"].startswith("sk-")

        agent_id = body["agent_id"]
        detail = client.get(f"/api/v1/agents/{agent_id}", headers=_bearer(token)).json()
        assert detail["agent"]["api_key_secret_arn"] is not None
        # The raw value is never echoed back by GET — only the ARN.
        assert "api_key" not in detail["agent"] or detail["agent"].get("api_key") is None


async def test_create_agent_in_enterprise_mode_does_not_provision_api_key(
    make_user_and_token, monkeypatch
) -> None:
    monkeypatch.setattr(settings, "deployment_mode", "enterprise")
    _, token = await make_user_and_token(TENANT_A, role="developer")

    with TestClient(app) as client:
        response = client.post(
            "/api/v1/agents", json=_minimal_agent_payload(), headers=_bearer(token)
        )
        assert response.status_code == 201, response.text
        assert response.json()["api_key"] is None

        agent_id = response.json()["agent_id"]
        detail = client.get(f"/api/v1/agents/{agent_id}", headers=_bearer(token)).json()
        assert detail["agent"]["api_key_secret_arn"] is None


async def test_rotate_generates_new_key_and_moves_old_arn_to_previous(
    make_user_and_token,
) -> None:
    _, token = await make_user_and_token(TENANT_A, role="developer")

    with TestClient(app) as client:
        created = client.post(
            "/api/v1/agents", json=_minimal_agent_payload(), headers=_bearer(token)
        ).json()
        agent_id = created["agent_id"]
        original_key = created["api_key"]

        before_rotate = time.time()
        rotate_response = client.post(
            f"/api/v1/agents/{agent_id}/credentials/rotate", headers=_bearer(token)
        )
        assert rotate_response.status_code == 200, rotate_response.text
        rotated = rotate_response.json()

        assert rotated["api_key"] != original_key
        assert rotated["api_key"].startswith("sk-")
        assert rotated["agent_id"] == agent_id
        assert rotated["api_key_secret_arn"]
        assert rotated["rotated_at"]

        detail = client.get(f"/api/v1/agents/{agent_id}", headers=_bearer(token)).json()["agent"]
        assert detail["api_key_secret_arn"] == rotated["api_key_secret_arn"]
        assert detail["previous_api_key_secret_arn"] is not None
        # Grace window is ~24h out from the rotation call, epoch seconds.
        assert detail["previous_key_expires_at"] > before_rotate + (23 * 3600)
        assert detail["previous_key_expires_at"] <= before_rotate + (24 * 3600) + 5


async def test_rotate_in_enterprise_mode_is_forbidden(make_user_and_token, monkeypatch) -> None:
    _, token = await make_user_and_token(TENANT_A, role="developer")

    with TestClient(app) as client:
        created = client.post(
            "/api/v1/agents", json=_minimal_agent_payload(), headers=_bearer(token)
        ).json()
        agent_id = created["agent_id"]

        monkeypatch.setattr(settings, "deployment_mode", "enterprise")
        response = client.post(
            f"/api/v1/agents/{agent_id}/credentials/rotate", headers=_bearer(token)
        )
        assert response.status_code == 403


async def test_rotate_with_no_provisioned_key_is_409(make_user_and_token, monkeypatch) -> None:
    """An agent created in enterprise mode has no api_key_secret_arn to
    rotate — even after switching back to prototype mode, rotate must
    still refuse, since there is nothing to move into previous_*."""
    monkeypatch.setattr(settings, "deployment_mode", "enterprise")
    _, token = await make_user_and_token(TENANT_A, role="developer")

    with TestClient(app) as client:
        created = client.post(
            "/api/v1/agents", json=_minimal_agent_payload(), headers=_bearer(token)
        ).json()
        agent_id = created["agent_id"]

        monkeypatch.setattr(settings, "deployment_mode", "prototype")
        response = client.post(
            f"/api/v1/agents/{agent_id}/credentials/rotate", headers=_bearer(token)
        )
        assert response.status_code == 409


async def test_rotate_unknown_agent_is_404(make_user_and_token) -> None:
    _, token = await make_user_and_token(TENANT_A, role="developer")

    with TestClient(app) as client:
        response = client.post(
            "/api/v1/agents/does-not-exist/credentials/rotate", headers=_bearer(token)
        )
        assert response.status_code == 404


async def test_rotate_cross_tenant_returns_404(make_user_and_token) -> None:
    _, token_a = await make_user_and_token(TENANT_A, role="developer")
    _, token_b = await make_user_and_token(TENANT_B, role="developer")

    with TestClient(app) as client:
        created = client.post(
            "/api/v1/agents", json=_minimal_agent_payload(), headers=_bearer(token_a)
        ).json()
        agent_id = created["agent_id"]

        response = client.post(
            f"/api/v1/agents/{agent_id}/credentials/rotate", headers=_bearer(token_b)
        )
        assert response.status_code == 404


async def test_rotate_forbidden_for_auditor_role(make_user_and_token) -> None:
    _, dev_token = await make_user_and_token(TENANT_A, role="developer")
    _, auditor_token = await make_user_and_token(TENANT_A, role="auditor")

    with TestClient(app) as client:
        created = client.post(
            "/api/v1/agents", json=_minimal_agent_payload(), headers=_bearer(dev_token)
        ).json()
        agent_id = created["agent_id"]

        response = client.post(
            f"/api/v1/agents/{agent_id}/credentials/rotate", headers=_bearer(auditor_token)
        )
        assert response.status_code == 403


async def test_revoke_sets_flag_and_is_idempotent(make_user_and_token) -> None:
    _, token = await make_user_and_token(TENANT_A, role="developer")

    with TestClient(app) as client:
        created = client.post(
            "/api/v1/agents", json=_minimal_agent_payload(), headers=_bearer(token)
        ).json()
        agent_id = created["agent_id"]

        first = client.post(
            f"/api/v1/agents/{agent_id}/credentials/revoke", headers=_bearer(token)
        )
        assert first.status_code == 200, first.text
        assert first.json()["api_key_revoked"] is True

        # Idempotent — a second revoke is still a 200 success, not a conflict.
        second = client.post(
            f"/api/v1/agents/{agent_id}/credentials/revoke", headers=_bearer(token)
        )
        assert second.status_code == 200
        assert second.json()["api_key_revoked"] is True

        detail = client.get(f"/api/v1/agents/{agent_id}", headers=_bearer(token)).json()["agent"]
        assert detail["api_key_revoked"] is True
        # Revocation leaves the ARN in place — a subsequent rotate still has
        # something to move into previous_api_key_secret_arn.
        assert detail["api_key_secret_arn"] is not None


async def test_revoke_unknown_agent_is_404(make_user_and_token) -> None:
    _, token = await make_user_and_token(TENANT_A, role="developer")

    with TestClient(app) as client:
        response = client.post(
            "/api/v1/agents/does-not-exist/credentials/revoke", headers=_bearer(token)
        )
        assert response.status_code == 404


async def test_revoke_cross_tenant_returns_404(make_user_and_token) -> None:
    _, token_a = await make_user_and_token(TENANT_A, role="developer")
    _, token_b = await make_user_and_token(TENANT_B, role="developer")

    with TestClient(app) as client:
        created = client.post(
            "/api/v1/agents", json=_minimal_agent_payload(), headers=_bearer(token_a)
        ).json()
        agent_id = created["agent_id"]

        response = client.post(
            f"/api/v1/agents/{agent_id}/credentials/revoke", headers=_bearer(token_b)
        )
        assert response.status_code == 404
