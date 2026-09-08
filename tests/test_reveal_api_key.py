"""Sprint 4 Phase 1 — Secure Reveal Endpoint (S-06, CLAUDE.md Section
56.7/57, R60-R63).

POST /api/v1/agents/{agent_id}/integration/reveal-api-key
"""

from __future__ import annotations

from typing import Any

import boto3
import fakeredis
from fastapi.testclient import TestClient

from app.config import settings
from app.main import app

TENANT_A = "tenant-a"
TENANT_B = "tenant-b"


def _events_for_agent(tenant_id: str, agent_id: str) -> list[dict[str, Any]]:
    dynamodb = boto3.resource("dynamodb", region_name="eu-west-2")
    table = dynamodb.Table(settings.dynamodb_audit_log_table)
    response = table.query(
        KeyConditionExpression=boto3.dynamodb.conditions.Key("tenant_id").eq(tenant_id)
    )
    return [item for item in response.get("Items", []) if item.get("agent_id") == agent_id]


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _minimal_agent_payload(name: str = "Reveal Test Agent") -> dict[str, Any]:
    return {
        "name": name,
        "description": "Agent used to exercise the reveal endpoint",
        "business_purpose": "Testing",
        "agent_type": "standard",
        "configuration": {
            "model_id": "anthropic.claude-3-5-sonnet-20241022-v2:0",
            "model_provider": "bedrock",
            "system_prompt": "You are a test agent.",
        },
    }


async def test_reveal_returns_current_api_key(make_user_and_token) -> None:
    _, token = await make_user_and_token(TENANT_A, role="developer")

    with TestClient(app) as client:
        app.state.redis_client = fakeredis.FakeAsyncRedis()
        created = client.post(
            "/api/v1/agents", json=_minimal_agent_payload(), headers=_bearer(token)
        ).json()
        agent_id = created["agent_id"]

        response = client.post(
            f"/api/v1/agents/{agent_id}/integration/reveal-api-key", headers=_bearer(token)
        )

        assert response.status_code == 200, response.text
        body = response.json()
        assert body["agent_id"] == agent_id
        assert body["api_key"] == created["api_key"]


async def test_reveal_forbidden_in_enterprise_mode(make_user_and_token, monkeypatch) -> None:
    _, token = await make_user_and_token(TENANT_A, role="developer")

    with TestClient(app) as client:
        app.state.redis_client = fakeredis.FakeAsyncRedis()
        created = client.post(
            "/api/v1/agents", json=_minimal_agent_payload(), headers=_bearer(token)
        ).json()
        agent_id = created["agent_id"]

        monkeypatch.setattr(settings, "deployment_mode", "enterprise")
        response = client.post(
            f"/api/v1/agents/{agent_id}/integration/reveal-api-key", headers=_bearer(token)
        )

        assert response.status_code == 403


async def test_reveal_with_no_provisioned_key_is_409(make_user_and_token, monkeypatch) -> None:
    monkeypatch.setattr(settings, "deployment_mode", "enterprise")
    _, token = await make_user_and_token(TENANT_A, role="developer")

    with TestClient(app) as client:
        app.state.redis_client = fakeredis.FakeAsyncRedis()
        created = client.post(
            "/api/v1/agents", json=_minimal_agent_payload(), headers=_bearer(token)
        ).json()
        agent_id = created["agent_id"]

        monkeypatch.setattr(settings, "deployment_mode", "prototype")
        response = client.post(
            f"/api/v1/agents/{agent_id}/integration/reveal-api-key", headers=_bearer(token)
        )

        assert response.status_code == 409


async def test_reveal_unknown_agent_is_404(make_user_and_token) -> None:
    _, token = await make_user_and_token(TENANT_A, role="developer")

    with TestClient(app) as client:
        app.state.redis_client = fakeredis.FakeAsyncRedis()
        response = client.post(
            "/api/v1/agents/does-not-exist/integration/reveal-api-key", headers=_bearer(token)
        )

        assert response.status_code == 404


async def test_reveal_cross_tenant_returns_404(make_user_and_token) -> None:
    _, token_a = await make_user_and_token(TENANT_A, role="developer")
    _, token_b = await make_user_and_token(TENANT_B, role="developer")

    with TestClient(app) as client:
        app.state.redis_client = fakeredis.FakeAsyncRedis()
        created = client.post(
            "/api/v1/agents", json=_minimal_agent_payload(), headers=_bearer(token_a)
        ).json()
        agent_id = created["agent_id"]

        response = client.post(
            f"/api/v1/agents/{agent_id}/integration/reveal-api-key", headers=_bearer(token_b)
        )

        assert response.status_code == 404


async def test_reveal_forbidden_for_auditor_role(make_user_and_token) -> None:
    _, dev_token = await make_user_and_token(TENANT_A, role="developer")
    _, auditor_token = await make_user_and_token(TENANT_A, role="auditor")

    with TestClient(app) as client:
        app.state.redis_client = fakeredis.FakeAsyncRedis()
        created = client.post(
            "/api/v1/agents", json=_minimal_agent_payload(), headers=_bearer(dev_token)
        ).json()
        agent_id = created["agent_id"]

        response = client.post(
            f"/api/v1/agents/{agent_id}/integration/reveal-api-key", headers=_bearer(auditor_token)
        )

        assert response.status_code == 403


async def test_reveal_rate_limited_after_three_calls_in_an_hour(make_user_and_token) -> None:
    _, token = await make_user_and_token(TENANT_A, role="developer")

    with TestClient(app) as client:
        app.state.redis_client = fakeredis.FakeAsyncRedis()
        created = client.post(
            "/api/v1/agents", json=_minimal_agent_payload(), headers=_bearer(token)
        ).json()
        agent_id = created["agent_id"]

        for _ in range(3):
            response = client.post(
                f"/api/v1/agents/{agent_id}/integration/reveal-api-key", headers=_bearer(token)
            )
            assert response.status_code == 200

        fourth = client.post(
            f"/api/v1/agents/{agent_id}/integration/reveal-api-key", headers=_bearer(token)
        )
        assert fourth.status_code == 429


async def test_reveal_fails_open_when_redis_unreachable(make_user_and_token) -> None:
    class _ExplodingRedis:
        async def incr(self, key: str) -> int:
            raise ConnectionError("redis unavailable")

        async def aclose(self) -> None:
            """No-op — app.main's lifespan shutdown calls this unconditionally."""

    _, token = await make_user_and_token(TENANT_A, role="developer")

    with TestClient(app) as client:
        created = client.post(
            "/api/v1/agents", json=_minimal_agent_payload(), headers=_bearer(token)
        ).json()
        agent_id = created["agent_id"]

        app.state.redis_client = _ExplodingRedis()
        response = client.post(
            f"/api/v1/agents/{agent_id}/integration/reveal-api-key", headers=_bearer(token)
        )

        assert response.status_code == 200


async def test_reveal_writes_credential_revealed_audit_event_with_source_ip(
    make_user_and_token,
) -> None:
    _, token = await make_user_and_token(TENANT_A, role="developer")

    with TestClient(app) as client:
        app.state.redis_client = fakeredis.FakeAsyncRedis()
        created = client.post(
            "/api/v1/agents", json=_minimal_agent_payload(), headers=_bearer(token)
        ).json()
        agent_id = created["agent_id"]

        response = client.post(
            f"/api/v1/agents/{agent_id}/integration/reveal-api-key", headers=_bearer(token)
        )
        assert response.status_code == 200

    events = _events_for_agent(TENANT_A, agent_id)
    revealed = [e for e in events if e["event_type"] == "credential.revealed"]
    assert len(revealed) == 1
    assert revealed[0]["result"] == "success"
    assert revealed[0]["source_ip"]
