"""API tests for /api/v1/platform/guardrail-policies (CLAUDE.md Section
37.7/37.10/37.11 — 2026-08-16 nested schema expansion).

Reads open to every role; writes (create/update/delete) admin-only. The
Bedrock auto-provisioning dependency is overridden with
FakeBedrockControlPlaneClient (tests/fakes.py) — moto 5.0.28 doesn't
implement create_guardrail/update_guardrail.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from app.dependencies import get_bedrock_guardrail_provisioner
from app.main import app
from app.modules.guardrails.provisioner import BedrockGuardrailProvisioner
from tests.fakes import (
    FailingBedrockControlPlaneClient,
    FakeBedrockControlPlaneClient,
    InvalidCredentialsBedrockControlPlaneClient,
)

TENANT_A = "tenant-a"
TENANT_B = "tenant-b"


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture(autouse=True)
def fake_bedrock_provisioner() -> Iterator[FakeBedrockControlPlaneClient]:
    client = FakeBedrockControlPlaneClient()
    app.dependency_overrides[get_bedrock_guardrail_provisioner] = (
        lambda: BedrockGuardrailProvisioner(client)
    )
    try:
        yield client
    finally:
        del app.dependency_overrides[get_bedrock_guardrail_provisioner]


async def test_admin_can_create_and_get_policy(
    make_user_and_token, fake_bedrock_provisioner: FakeBedrockControlPlaneClient
) -> None:
    _, admin_token = await make_user_and_token(TENANT_A, role="admin")

    with TestClient(app) as client:
        created = client.post(
            "/api/v1/platform/guardrail-policies",
            json={"name": "Strict", "description": "d"},
            headers=_bearer(admin_token),
        )
        assert created.status_code == 201
        body = created.json()
        assert body["bert"]["block_threshold"] == 0.85
        assert body["bedrock_guardrail_id"] == "gr-fake-123"
        assert body["bedrock_guardrail_version"] == "DRAFT"
        policy_id = body["policy_id"]

        fetched = client.get(
            f"/api/v1/platform/guardrail-policies/{policy_id}", headers=_bearer(admin_token)
        )
        assert fetched.status_code == 200
        assert fetched.json()["bedrock_guardrail_id"] == "gr-fake-123"

    assert len(fake_bedrock_provisioner.create_calls) == 1
    assert fake_bedrock_provisioner.create_calls[0]["name"] == policy_id


async def test_create_skips_provisioning_when_bedrock_disabled(
    make_user_and_token, fake_bedrock_provisioner: FakeBedrockControlPlaneClient
) -> None:
    _, admin_token = await make_user_and_token(TENANT_A, role="admin")

    with TestClient(app) as client:
        created = client.post(
            "/api/v1/platform/guardrail-policies",
            json={"name": "No Bedrock", "description": "d", "bedrock_enabled": False},
            headers=_bearer(admin_token),
        )

    assert created.status_code == 201
    assert created.json()["bedrock_guardrail_id"] is None
    assert fake_bedrock_provisioner.create_calls == []


async def test_update_calls_update_guardrail_not_create(
    make_user_and_token, fake_bedrock_provisioner: FakeBedrockControlPlaneClient
) -> None:
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
    assert updated.json()["description"] == "updated description"
    assert len(fake_bedrock_provisioner.create_calls) == 1  # from create
    assert len(fake_bedrock_provisioner.update_calls) == 1  # from the PUT
    assert fake_bedrock_provisioner.update_calls[0]["guardrailIdentifier"] == "gr-fake-123"


async def test_create_failure_returns_502_and_does_not_orphan_record(
    make_user_and_token,
) -> None:
    """A real Bedrock provisioning failure (auth, throttling, region not
    enabled) must surface as a clean 502 — not a bare 500 — and must not
    leave a DynamoDB record behind that the caller was told failed to
    save (this is exactly what happens today against real Bedrock with
    local dev's placeholder AWS credentials, CLAUDE.md Section 34)."""
    _, admin_token = await make_user_and_token(TENANT_A, role="admin")
    # Overwrites — does not delete — the autouse fake_bedrock_provisioner
    # fixture's own override; that fixture's teardown still does the one
    # `del` afterward, so this must NOT also delete it here.
    app.dependency_overrides[get_bedrock_guardrail_provisioner] = (
        lambda: BedrockGuardrailProvisioner(FailingBedrockControlPlaneClient())
    )

    with TestClient(app) as client:
        created = client.post(
            "/api/v1/platform/guardrail-policies",
            json={"name": "Will Fail", "description": "d"},
            headers=_bearer(admin_token),
        )
        assert created.status_code == 502
        assert "Bedrock guardrail provisioning failed" in created.json()["detail"]

        listed = client.get("/api/v1/platform/guardrail-policies", headers=_bearer(admin_token))
        assert listed.json()["items"] == []


async def test_create_failure_with_credential_error_returns_503(make_user_and_token) -> None:
    """QA A-05 — a real botocore ClientError shaped like AWS's actual
    'invalid/unrecognized credentials' response must surface as an
    actionable 503, distinct from the generic 502 used for any other
    Bedrock-side failure."""
    _, admin_token = await make_user_and_token(TENANT_A, role="admin")
    app.dependency_overrides[get_bedrock_guardrail_provisioner] = (
        lambda: BedrockGuardrailProvisioner(InvalidCredentialsBedrockControlPlaneClient())
    )

    with TestClient(app) as client:
        created = client.post(
            "/api/v1/platform/guardrail-policies",
            json={"name": "Will Fail On Credentials", "description": "d"},
            headers=_bearer(admin_token),
        )
        assert created.status_code == 503
        body = created.json()["detail"]
        assert body["error"] == "aws_credentials_invalid"
        assert "MOCK_BEDROCK_GUARDRAILS" in body["message"]

        listed = client.get("/api/v1/platform/guardrail-policies", headers=_bearer(admin_token))
        assert listed.json()["items"] == []


async def test_mock_bedrock_guardrails_never_calls_the_real_client(make_user_and_token) -> None:
    """QA A-05 — settings.mock_bedrock_guardrails must short-circuit before
    any AWS call, so it works against local dev's placeholder credentials.
    Uses FailingBedrockControlPlaneClient as the underlying client
    precisely so the test fails loudly if mock mode ever falls through to
    a real call."""
    _, admin_token = await make_user_and_token(TENANT_A, role="admin")
    app.dependency_overrides[get_bedrock_guardrail_provisioner] = (
        lambda: BedrockGuardrailProvisioner(
            FailingBedrockControlPlaneClient(), mock_enabled=True
        )
    )

    with TestClient(app) as client:
        created = client.post(
            "/api/v1/platform/guardrail-policies",
            json={"name": "Mocked Policy", "description": "d"},
            headers=_bearer(admin_token),
        )
        assert created.status_code == 201
        assert created.json()["bedrock_guardrail_id"].startswith("mock-gr-")


async def test_update_provisioning_failure_returns_502(make_user_and_token) -> None:
    _, admin_token = await make_user_and_token(TENANT_A, role="admin")

    with TestClient(app) as client:
        created = client.post(
            "/api/v1/platform/guardrail-policies",
            json={"name": "Editable", "description": "d"},
            headers=_bearer(admin_token),
        )
        policy_id = created.json()["policy_id"]

        # Overwrites — does not delete — the autouse fixture's override; see
        # the create-failure test above for why no `del` belongs here.
        app.dependency_overrides[get_bedrock_guardrail_provisioner] = (
            lambda: BedrockGuardrailProvisioner(FailingBedrockControlPlaneClient())
        )
        updated = client.put(
            f"/api/v1/platform/guardrail-policies/{policy_id}",
            json={"description": "updated description"},
            headers=_bearer(admin_token),
        )
        assert updated.status_code == 502
        assert "Bedrock guardrail provisioning failed" in updated.json()["detail"]


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
            json={"name": "TooLoose", "description": "d", "bert": {"block_threshold": 0.5}},
            headers=_bearer(admin_token),
        )
    assert response.status_code == 422
    fields = {e["field"] for e in response.json()["detail"]}
    assert "bert.block_threshold" in fields


async def test_create_rejects_escalate_threshold_above_ceiling(make_user_and_token) -> None:
    _, admin_token = await make_user_and_token(TENANT_A, role="admin")

    with TestClient(app) as client:
        response = client.post(
            "/api/v1/platform/guardrail-policies",
            json={"name": "TooEager", "description": "d", "bert": {"escalate_threshold": 0.9}},
            headers=_bearer(admin_token),
        )
    assert response.status_code == 422
    fields = {e["field"] for e in response.json()["detail"]}
    assert "bert.escalate_threshold" in fields


async def test_create_rejects_block_not_greater_than_escalate(make_user_and_token) -> None:
    _, admin_token = await make_user_and_token(TENANT_A, role="admin")

    with TestClient(app) as client:
        response = client.post(
            "/api/v1/platform/guardrail-policies",
            json={
                "name": "Inverted",
                "description": "d",
                "bert": {"block_threshold": 0.75, "escalate_threshold": 0.75},
            },
            headers=_bearer(admin_token),
        )
    assert response.status_code == 422


async def test_update_partial_replaces_whole_bert_section(make_user_and_token) -> None:
    _, admin_token = await make_user_and_token(TENANT_A, role="admin")

    with TestClient(app) as client:
        created = client.post(
            "/api/v1/platform/guardrail-policies",
            json={"name": "Sectioned", "description": "d"},
            headers=_bearer(admin_token),
        )
        policy_id = created.json()["policy_id"]

        updated = client.put(
            f"/api/v1/platform/guardrail-policies/{policy_id}",
            json={"bert": {"block_threshold": 0.90, "escalate_threshold": 0.50}},
            headers=_bearer(admin_token),
        )
    assert updated.status_code == 200
    body = updated.json()
    assert body["bert"]["block_threshold"] == 0.90
    # replacing the section resets sub-fields not explicitly re-sent, back
    # to BertConfig's own defaults (check_toxicity=True etc.) — a whole-
    # section replace, not a field merge.
    assert body["bert"]["check_toxicity"] is True


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
            json={"bert": {"block_threshold": 0.6}},
            headers=_bearer(admin_token),
        )
    assert response.status_code == 422


async def test_nested_pii_topics_keywords_persist(make_user_and_token) -> None:
    _, admin_token = await make_user_and_token(TENANT_A, role="admin")

    with TestClient(app) as client:
        created = client.post(
            "/api/v1/platform/guardrail-policies",
            json={
                "name": "Full",
                "description": "d",
                "pii": {"email": {"action": "BLOCK"}},
                "topics": {"banned_topics": ["politics"]},
                "keywords": {"rules": [{"pattern": "jailbreak", "action": "BLOCK"}]},
                "compliance": {"frameworks": ["GDPR", "HIPAA"]},
                "blocked_messages": {"content_blocked": "Nope."},
            },
            headers=_bearer(admin_token),
        )

    assert created.status_code == 201
    body = created.json()
    assert body["pii"]["email"]["action"] == "BLOCK"
    assert body["topics"]["banned_topics"] == ["politics"]
    assert body["keywords"]["rules"][0]["pattern"] == "jailbreak"
    assert set(body["compliance"]["frameworks"]) == {"GDPR", "HIPAA"}
    assert body["blocked_messages"]["content_blocked"] == "Nope."


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
                "agent_type": "standard",
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
