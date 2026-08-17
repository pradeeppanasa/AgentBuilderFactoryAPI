"""API tests for /api/v1/platform/bedrock-credentials (CLAUDE.md Section
37.14/37.15 — STS AssumeRole credential bindings for cross-account Bedrock
guardrail provisioning).

Admin-only write; read open to every role. Delete is blocked (409) while
any guardrail policy still references the credential — same
reference-check pattern as the Knowledge Base and Guardrail Policy
libraries.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.main import app

TENANT_A = "tenant-a"
TENANT_B = "tenant-b"


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def test_admin_can_create_list_get_credential(make_user_and_token) -> None:
    _, admin_token = await make_user_and_token(TENANT_A, role="admin")

    with TestClient(app) as client:
        created = client.post(
            "/api/v1/platform/bedrock-credentials",
            json={"name": "Cross-account", "role_arn": "arn:aws:iam::999999999999:role/x"},
            headers=_bearer(admin_token),
        )
        assert created.status_code == 201
        body = created.json()
        assert body["name"] == "Cross-account"
        assert body["role_arn"] == "arn:aws:iam::999999999999:role/x"
        credential_id = body["credential_id"]

        fetched = client.get(
            f"/api/v1/platform/bedrock-credentials/{credential_id}", headers=_bearer(admin_token)
        )
        assert fetched.status_code == 200
        assert fetched.json()["credential_id"] == credential_id

        listed = client.get("/api/v1/platform/bedrock-credentials", headers=_bearer(admin_token))
        assert listed.status_code == 200
        assert [item["credential_id"] for item in listed.json()["items"]] == [credential_id]


async def test_non_admin_can_read_but_not_write(make_user_and_token) -> None:
    _, admin_token = await make_user_and_token(TENANT_A, role="admin")
    _, developer_token = await make_user_and_token(TENANT_A, role="developer")

    with TestClient(app) as client:
        created = client.post(
            "/api/v1/platform/bedrock-credentials",
            json={"name": "Cross-account", "role_arn": "arn:aws:iam::999999999999:role/x"},
            headers=_bearer(admin_token),
        )
        credential_id = created.json()["credential_id"]

        denied_create = client.post(
            "/api/v1/platform/bedrock-credentials",
            json={"name": "Another", "role_arn": "arn:aws:iam::999999999999:role/y"},
            headers=_bearer(developer_token),
        )
        assert denied_create.status_code == 403

        allowed_read = client.get(
            f"/api/v1/platform/bedrock-credentials/{credential_id}",
            headers=_bearer(developer_token),
        )
        assert allowed_read.status_code == 200

        denied_delete = client.delete(
            f"/api/v1/platform/bedrock-credentials/{credential_id}",
            headers=_bearer(developer_token),
        )
        assert denied_delete.status_code == 403


async def test_get_unknown_credential_returns_404(make_user_and_token) -> None:
    _, admin_token = await make_user_and_token(TENANT_A, role="admin")

    with TestClient(app) as client:
        response = client.get(
            "/api/v1/platform/bedrock-credentials/does-not-exist", headers=_bearer(admin_token)
        )
        assert response.status_code == 404


async def test_delete_removes_unreferenced_credential(make_user_and_token) -> None:
    _, admin_token = await make_user_and_token(TENANT_A, role="admin")

    with TestClient(app) as client:
        created = client.post(
            "/api/v1/platform/bedrock-credentials",
            json={"name": "Cross-account", "role_arn": "arn:aws:iam::999999999999:role/x"},
            headers=_bearer(admin_token),
        )
        credential_id = created.json()["credential_id"]

        deleted = client.delete(
            f"/api/v1/platform/bedrock-credentials/{credential_id}", headers=_bearer(admin_token)
        )
        assert deleted.status_code == 204

        after = client.get(
            f"/api/v1/platform/bedrock-credentials/{credential_id}", headers=_bearer(admin_token)
        )
        assert after.status_code == 404


async def test_delete_blocked_while_referenced_by_guardrail_policy(make_user_and_token) -> None:
    from app.dependencies import get_bedrock_guardrail_provisioner
    from app.modules.guardrails.provisioner import BedrockGuardrailProvisioner
    from tests.fakes import FakeBedrockControlPlaneClient

    _, admin_token = await make_user_and_token(TENANT_A, role="admin")

    fake_client = FakeBedrockControlPlaneClient()
    app.dependency_overrides[get_bedrock_guardrail_provisioner] = (
        lambda: BedrockGuardrailProvisioner(fake_client)
    )
    try:
        with TestClient(app) as client:
            credential = client.post(
                "/api/v1/platform/bedrock-credentials",
                json={"name": "Cross-account", "role_arn": "arn:aws:iam::999999999999:role/x"},
                headers=_bearer(admin_token),
            ).json()

            # bedrock_enabled=False: this test is about the reference index
            # (bedrock_credential_id is stored on the policy either way), not
            # about actually resolving STS credentials — the app's real
            # get_bedrock_guardrail_provisioner dependency has no
            # credential_store/sts_client wired for a fake credential_id.
            client.post(
                "/api/v1/platform/guardrail-policies",
                json={
                    "name": "Uses credential",
                    "description": "d",
                    "bedrock_enabled": False,
                    "bedrock_credential_id": credential["credential_id"],
                },
                headers=_bearer(admin_token),
            )

            denied = client.delete(
                f"/api/v1/platform/bedrock-credentials/{credential['credential_id']}",
                headers=_bearer(admin_token),
            )
            assert denied.status_code == 409
            assert credential["credential_id"] in denied.json()["detail"]
    finally:
        del app.dependency_overrides[get_bedrock_guardrail_provisioner]


async def test_credentials_are_tenant_isolated(make_user_and_token) -> None:
    _, admin_a_token = await make_user_and_token(TENANT_A, role="admin")
    _, admin_b_token = await make_user_and_token(TENANT_B, role="admin")

    with TestClient(app) as client:
        created = client.post(
            "/api/v1/platform/bedrock-credentials",
            json={"name": "Tenant A credential", "role_arn": "arn:aws:iam::999999999999:role/x"},
            headers=_bearer(admin_a_token),
        ).json()

        cross_tenant_get = client.get(
            f"/api/v1/platform/bedrock-credentials/{created['credential_id']}",
            headers=_bearer(admin_b_token),
        )
        assert cross_tenant_get.status_code == 404

        tenant_b_list = client.get(
            "/api/v1/platform/bedrock-credentials", headers=_bearer(admin_b_token)
        )
        assert tenant_b_list.json()["items"] == []
