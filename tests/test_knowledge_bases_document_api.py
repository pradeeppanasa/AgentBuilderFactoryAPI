"""API tests for the Knowledge Base document upload/list/delete and sync
trigger/status endpoints (instructions_kb_api.md / CLAUDE.md Section 43),
plus the presigned-upload / validate-s3 / "Sync from existing S3 path"
endpoints (CLAUDE.md Section 47, R59 corrected 2026-09-01).

A KB is a standalone platform resource — every endpoint here is exercised
with NO agent ever created or deployed. The only place an agent shows up
in this file is `_create_agent_referencing_kb`, used solely to test the
unrelated "can't delete a KB still referenced by an agent" guard.

Real S3 calls run against moto's in-memory backend (already globally mocked
by conftest.py's session-scoped mock_aws() — only the bucket needs
creating here). Bedrock KB/data-source/ingestion-job calls are not
implemented by moto (same gap FakeBedrockControlPlaneClient documents for
plain `bedrock`), so BedrockKnowledgeBaseProvisioner is given a
FakeBedrockAgentClient via dependency override.
"""

from __future__ import annotations

import contextlib
from collections.abc import Iterator
from typing import Any

import boto3
import pytest
from fastapi.testclient import TestClient

from app.config import settings
from app.dependencies import get_bedrock_kb_provisioner
from app.main import app
from app.modules.knowledge_base.provisioner import BedrockKnowledgeBaseProvisioner
from tests.fakes import FakeBedrockAgentClient

TENANT_A = "tenant-a"
TEST_KB_BUCKET = "panasa-kb-documents-test"


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _ensure_bucket(name: str = TEST_KB_BUCKET) -> None:
    s3 = boto3.client("s3", region_name="eu-west-2")
    with contextlib.suppress(s3.exceptions.BucketAlreadyOwnedByYou):
        s3.create_bucket(
            Bucket=name, CreateBucketConfiguration={"LocationConstraint": "eu-west-2"}
        )


@pytest.fixture(autouse=True)
def _kb_bucket_configured(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """The local-dev env var fallback (CLAUDE.md Section 47.1) — every test
    in this file gets a working bucket via this unless it explicitly tests
    the "nothing configured at all" 409 path."""
    _ensure_bucket()
    monkeypatch.setattr(settings, "kb_documents_bucket", TEST_KB_BUCKET)
    yield


@pytest.fixture
def fake_bedrock_agent() -> Iterator[FakeBedrockAgentClient]:
    client = FakeBedrockAgentClient()
    app.dependency_overrides[get_bedrock_kb_provisioner] = lambda: BedrockKnowledgeBaseProvisioner(
        client,
        kb_role_arn="arn:aws:iam::123456789012:role/panasa-bedrock-kb-role",
        opensearch_collection_arn="arn:aws:aoss:eu-west-2:123456789012:collection/fake",
        aws_region="eu-west-2",
        kb_documents_bucket=TEST_KB_BUCKET,
    )
    try:
        yield client
    finally:
        del app.dependency_overrides[get_bedrock_kb_provisioner]


def _create_kb(
    client: TestClient,
    token: str,
    name: str = "Payroll Policy Documents",
    source_type: str = "manual",
    source_config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    response = client.post(
        "/api/v1/platform/knowledge-bases",
        json={
            "name": name,
            "description": "Compliance docs",
            "source_type": source_type,
            "source_config": source_config or {},
        },
        headers=_bearer(token),
    )
    assert response.status_code == 201, response.text
    return dict(response.json())


async def _create_agent_referencing_kb(
    client: TestClient, token: str, kb_id: str
) -> str:
    """Only used by the delete-guard test below — unrelated to R59. A KB's
    delete guard checks whether ANY agent's current version references it,
    regardless of that agent's deploy status, so this never needs to touch
    deployment at all."""
    response = client.post(
        "/api/v1/agents",
        json={
            "name": "KB Test Agent",
            "description": "References the knowledge base under test",
            "business_purpose": "Exercise the KB delete guard",
            "agent_type": "standard",
            "configuration": {
                "model_id": "anthropic.claude-3-5-sonnet-20241022-v2:0",
                "model_provider": "bedrock",
                "system_prompt": "You are a test agent.",
                "kb_id": kb_id,
            },
        },
        headers=_bearer(token),
    )
    assert response.status_code == 201, response.text
    return str(response.json()["agent_id"])


async def test_create_provisions_real_bedrock_kb_and_s3_prefix(
    make_user_and_token, fake_bedrock_agent: FakeBedrockAgentClient
) -> None:
    _, token = await make_user_and_token(TENANT_A, role="developer")

    with TestClient(app) as client:
        kb = _create_kb(client, token)

    assert kb["status"] == "ACTIVE"
    assert kb["bedrock_kb_id"] == "kb-fake-123"
    assert kb["bedrock_ds_id"] == "ds-fake-456"
    assert kb["s3_bucket"] == TEST_KB_BUCKET
    # Default "Settings -> Deployment -> S3 Folder Prefix" — no tenant_id,
    # no "panasa", nothing but a customer-configurable folder name.
    assert kb["s3_prefix"] == f"agent-factory/{kb['kb_id']}/raw/"
    assert len(fake_bedrock_agent.create_kb_calls) == 1
    assert len(fake_bedrock_agent.create_ds_calls) == 1


async def test_upload_document_lands_at_correct_s3_key(
    make_user_and_token, fake_bedrock_agent: FakeBedrockAgentClient
) -> None:
    _, token = await make_user_and_token(TENANT_A, role="developer")

    with TestClient(app) as client:
        kb = _create_kb(client, token)
        kb_id = kb["kb_id"]

        response = client.post(
            f"/api/v1/platform/knowledge-bases/{kb_id}/documents",
            files={"files": ("policy.pdf", b"%PDF-1.4 fake content", "application/pdf")},
            headers=_bearer(token),
        )
        assert response.status_code == 200
        body = response.json()
        assert body["count"] == 1
        assert body["uploaded"][0]["s3_key"] == f"agent-factory/{kb_id}/raw/policy.pdf"

        listed = client.get(
            f"/api/v1/platform/knowledge-bases/{kb_id}/documents", headers=_bearer(token)
        )
        assert listed.status_code == 200
        assert listed.json()["count"] == 1
        assert listed.json()["documents"][0]["filename"] == "policy.pdf"

        kb_after = client.get(
            f"/api/v1/platform/knowledge-bases/{kb_id}", headers=_bearer(token)
        ).json()
        assert kb_after["document_count"] == 1


async def test_upload_rejects_unsupported_file_type(
    make_user_and_token, fake_bedrock_agent: FakeBedrockAgentClient
) -> None:
    _, token = await make_user_and_token(TENANT_A, role="developer")

    with TestClient(app) as client:
        kb = _create_kb(client, token)

        response = client.post(
            f"/api/v1/platform/knowledge-bases/{kb['kb_id']}/documents",
            files={"files": ("sheet.xlsx", b"fake", "application/vnd.ms-excel")},
            headers=_bearer(token),
        )
    assert response.status_code == 415
    assert response.json()["detail"]["error"] == "unsupported_file_type"


async def test_delete_document_removes_it_from_s3(
    make_user_and_token, fake_bedrock_agent: FakeBedrockAgentClient
) -> None:
    _, token = await make_user_and_token(TENANT_A, role="developer")

    with TestClient(app) as client:
        kb = _create_kb(client, token)
        kb_id = kb["kb_id"]
        upload = client.post(
            f"/api/v1/platform/knowledge-bases/{kb_id}/documents",
            files={"files": ("policy.pdf", b"content", "application/pdf")},
            headers=_bearer(token),
        ).json()
        s3_key = upload["uploaded"][0]["s3_key"]

        deleted = client.delete(
            f"/api/v1/platform/knowledge-bases/{kb_id}/documents/{s3_key}",
            headers=_bearer(token),
        )
        assert deleted.status_code == 204

        listed = client.get(
            f"/api/v1/platform/knowledge-bases/{kb_id}/documents", headers=_bearer(token)
        )
        assert listed.json()["count"] == 0


async def test_sync_trigger_and_status(
    make_user_and_token, fake_bedrock_agent: FakeBedrockAgentClient
) -> None:
    _, token = await make_user_and_token(TENANT_A, role="developer")

    with TestClient(app) as client:
        kb = _create_kb(client, token)
        kb_id = kb["kb_id"]

        triggered = client.post(
            f"/api/v1/platform/knowledge-bases/{kb_id}/sync", headers=_bearer(token)
        )
        assert triggered.status_code == 202
        job_id = triggered.json()["ingestion_job_id"]
        assert triggered.json()["status"] == "IN_PROGRESS"

        status_response = client.get(
            f"/api/v1/platform/knowledge-bases/{kb_id}/sync/status",
            params={"ingestion_job_id": job_id},
            headers=_bearer(token),
        )
        assert status_response.status_code == 200
        body = status_response.json()
        assert body["status"] == "COMPLETE"
        assert body["documents_indexed"] == 3
        assert body["error"] is None

        kb_after = client.get(
            f"/api/v1/platform/knowledge-bases/{kb_id}", headers=_bearer(token)
        ).json()
        assert kb_after["sync_status"] == "COMPLETE"
        assert kb_after["last_synced_at"] is not None


async def test_sync_already_in_progress_returns_409(
    make_user_and_token, fake_bedrock_agent: FakeBedrockAgentClient
) -> None:
    _, token = await make_user_and_token(TENANT_A, role="developer")

    with TestClient(app) as client:
        kb = _create_kb(client, token)
        kb_id = kb["kb_id"]
        client.post(f"/api/v1/platform/knowledge-bases/{kb_id}/sync", headers=_bearer(token))

        second = client.post(
            f"/api/v1/platform/knowledge-bases/{kb_id}/sync", headers=_bearer(token)
        )
    assert second.status_code == 409
    assert second.json()["detail"]["error"] == "sync_in_progress"


async def test_delete_knowledge_base_deprovisions_bedrock_and_s3(
    make_user_and_token, fake_bedrock_agent: FakeBedrockAgentClient
) -> None:
    _, token = await make_user_and_token(TENANT_A, role="developer")

    with TestClient(app) as client:
        kb = _create_kb(client, token)
        kb_id = kb["kb_id"]
        agent_id = await _create_agent_referencing_kb(client, token, kb_id)
        client.post(
            f"/api/v1/platform/knowledge-bases/{kb_id}/documents",
            files={"files": ("policy.pdf", b"content", "application/pdf")},
            headers=_bearer(token),
        )

        # KB delete is blocked while any agent still references it (the
        # existing referencing-agent guard) — remove that reference before
        # deleting, same as any other real cleanup would have to.
        await app.state.registry_store.hard_delete_agent(TENANT_A, agent_id)

        deleted = client.delete(
            f"/api/v1/platform/knowledge-bases/{kb_id}", headers=_bearer(token)
        )
        assert deleted.status_code == 204

    assert len(fake_bedrock_agent.delete_ds_calls) == 1
    assert len(fake_bedrock_agent.delete_kb_calls) == 1


async def test_delete_knowledge_base_blocked_while_referenced_by_agent(
    make_user_and_token, fake_bedrock_agent: FakeBedrockAgentClient
) -> None:
    """The delete guard is the ONLY place agent state matters for a KB —
    and even here it's just "does any agent's config reference this
    kb_id", never a deploy-status check (CLAUDE.md Section 47.2)."""
    _, token = await make_user_and_token(TENANT_A, role="developer")

    with TestClient(app) as client:
        kb = _create_kb(client, token)
        kb_id = kb["kb_id"]
        agent_id = await _create_agent_referencing_kb(client, token, kb_id)

        response = client.delete(
            f"/api/v1/platform/knowledge-bases/{kb_id}", headers=_bearer(token)
        )
    assert response.status_code == 409
    assert agent_id in response.json()["detail"]


# ── Section 47 (R59 corrected 2026-09-01): a KB is a standalone platform
# resource. Uploads/sync are gated on nothing but "is a bucket configured"
# — never on any agent's deploy status. ──────────────────────────────────


async def test_upload_blocked_when_no_bucket_configured(
    make_user_and_token, fake_bedrock_agent: FakeBedrockAgentClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "kb_documents_bucket", None)
    _, token = await make_user_and_token(TENANT_A, role="developer")

    with TestClient(app) as client:
        kb = _create_kb(client, token)

        response = client.post(
            f"/api/v1/platform/knowledge-bases/{kb['kb_id']}/documents",
            files={"files": ("policy.pdf", b"content", "application/pdf")},
            headers=_bearer(token),
        )
    assert response.status_code == 409
    assert response.json()["detail"]["error"] == "s3_not_configured"
    assert "Settings" in response.json()["detail"]["message"]


async def test_sync_blocked_when_no_bucket_configured(
    make_user_and_token, fake_bedrock_agent: FakeBedrockAgentClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "kb_documents_bucket", None)
    _, token = await make_user_and_token(TENANT_A, role="developer")

    with TestClient(app) as client:
        kb = _create_kb(client, token)

        response = client.post(
            f"/api/v1/platform/knowledge-bases/{kb['kb_id']}/sync", headers=_bearer(token)
        )
    assert response.status_code == 409
    assert response.json()["detail"]["error"] == "s3_not_configured"


async def test_presigned_upload_returns_one_url_per_file(
    make_user_and_token, fake_bedrock_agent: FakeBedrockAgentClient
) -> None:
    """Bulk: one request, one presigned URL per file, uploaded in parallel
    by the browser (CLAUDE.md Section 47.3)."""
    _, token = await make_user_and_token(TENANT_A, role="developer")

    with TestClient(app) as client:
        kb = _create_kb(client, token)
        kb_id = kb["kb_id"]

        response = client.post(
            f"/api/v1/platform/knowledge-bases/{kb_id}/presigned-upload",
            json={
                "files": [
                    {"filename": "policy.pdf", "content_type": "application/pdf"},
                    {"filename": "handbook.docx"},
                ]
            },
            headers=_bearer(token),
        )
    assert response.status_code == 200
    body = response.json()
    assert body["bucket"] == TEST_KB_BUCKET
    assert body["expires_in_seconds"] == 900
    assert len(body["uploads"]) == 2
    filenames = {u["filename"] for u in body["uploads"]}
    assert filenames == {"policy.pdf", "handbook.docx"}
    for upload in body["uploads"]:
        assert upload["s3_key"] == f"agent-factory/{kb_id}/raw/{upload['filename']}"
        assert upload["upload_url"].startswith("http")


async def test_presigned_upload_blocked_when_no_bucket_configured(
    make_user_and_token, fake_bedrock_agent: FakeBedrockAgentClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "kb_documents_bucket", None)
    _, token = await make_user_and_token(TENANT_A, role="developer")

    with TestClient(app) as client:
        kb = _create_kb(client, token)

        response = client.post(
            f"/api/v1/platform/knowledge-bases/{kb['kb_id']}/presigned-upload",
            json={"files": [{"filename": "policy.pdf"}]},
            headers=_bearer(token),
        )
    assert response.status_code == 409
    assert response.json()["detail"]["error"] == "s3_not_configured"


async def test_presigned_upload_rejects_unsupported_file_type(
    make_user_and_token, fake_bedrock_agent: FakeBedrockAgentClient
) -> None:
    _, token = await make_user_and_token(TENANT_A, role="developer")

    with TestClient(app) as client:
        kb = _create_kb(client, token)
        kb_id = kb["kb_id"]

        response = client.post(
            f"/api/v1/platform/knowledge-bases/{kb_id}/presigned-upload",
            json={"files": [{"filename": "sheet.xlsx"}]},
            headers=_bearer(token),
        )
    assert response.status_code == 415
    assert response.json()["detail"]["error"] == "unsupported_file_type"


async def test_validate_s3_accessible_bucket(
    make_user_and_token, fake_bedrock_agent: FakeBedrockAgentClient
) -> None:
    _, token = await make_user_and_token(TENANT_A, role="developer")

    with TestClient(app) as client:
        kb = _create_kb(client, token)
        kb_id = kb["kb_id"]

        response = client.post(
            f"/api/v1/platform/knowledge-bases/{kb_id}/validate-s3",
            json={"bucket_name": TEST_KB_BUCKET},
            headers=_bearer(token),
        )
    assert response.status_code == 200
    assert response.json() == {"accessible": True, "bucket_name": TEST_KB_BUCKET}


async def test_validate_s3_inaccessible_bucket_returns_422(
    make_user_and_token, fake_bedrock_agent: FakeBedrockAgentClient
) -> None:
    _, token = await make_user_and_token(TENANT_A, role="developer")

    with TestClient(app) as client:
        kb = _create_kb(client, token)
        kb_id = kb["kb_id"]

        response = client.post(
            f"/api/v1/platform/knowledge-bases/{kb_id}/validate-s3",
            json={"bucket_name": "this-bucket-does-not-exist-anywhere"},
            headers=_bearer(token),
        )
    assert response.status_code == 422
    assert response.json()["detail"]["error"] == "bucket_not_accessible"


async def test_validate_s3_never_gated_on_kb_bucket_state(
    make_user_and_token, fake_bedrock_agent: FakeBedrockAgentClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """validate-s3 is a general "can this Runtime reach this bucket"
    utility (e.g. used from the Settings page before saving) — it works
    even for a KB that itself has no bucket configured yet."""
    monkeypatch.setattr(settings, "kb_documents_bucket", None)
    _, token = await make_user_and_token(TENANT_A, role="developer")

    with TestClient(app) as client:
        kb = _create_kb(client, token)
        assert kb["s3_bucket"] is None

        response = client.post(
            f"/api/v1/platform/knowledge-bases/{kb['kb_id']}/validate-s3",
            json={"bucket_name": TEST_KB_BUCKET},
            headers=_bearer(token),
        )
    assert response.status_code == 200


# ── Tenant "Settings -> Deployment -> Customer S3 Bucket / S3 Folder
# Prefix" (CLAUDE.md Section 47.1) ──────────────────────────────────────


async def test_tenant_configured_bucket_and_prefix_win_over_env_fallback(
    make_user_and_token, fake_bedrock_agent: FakeBedrockAgentClient
) -> None:
    tenant_bucket = "acme-customer-owned-bucket"
    _ensure_bucket(tenant_bucket)
    _, admin_token = await make_user_and_token(TENANT_A, role="admin")
    _, dev_token = await make_user_and_token(TENANT_A, role="developer")

    with TestClient(app) as client:
        saved = client.patch(
            "/api/v1/admin/settings/deployment",
            json={
                "default_approval_mode": "automated",
                "kb_s3_bucket": tenant_bucket,
                "kb_s3_prefix": "knowledge",
            },
            headers=_bearer(admin_token),
        )
        assert saved.status_code == 200
        assert saved.json()["kb_s3_bucket"] == tenant_bucket
        assert saved.json()["kb_s3_prefix"] == "knowledge"

        kb = _create_kb(client, dev_token)

    # Wins over the TEST_KB_BUCKET env-var fallback set by the autouse
    # fixture, and the prefix is neither "panasa" nor the tenant_id.
    assert kb["s3_bucket"] == tenant_bucket
    assert kb["s3_prefix"] == f"knowledge/{kb['kb_id']}/raw/"


# ── "Sync from existing S3 path" (source_type="s3") ─────────────────────


async def test_sync_from_existing_s3_path_uses_customer_path_verbatim(
    make_user_and_token, fake_bedrock_agent: FakeBedrockAgentClient
) -> None:
    customer_bucket = "acme-already-has-these-docs"
    _ensure_bucket(customer_bucket)
    _, token = await make_user_and_token(TENANT_A, role="developer")

    with TestClient(app) as client:
        kb = _create_kb(
            client,
            token,
            name="Existing Docs KB",
            source_type="s3",
            source_config={"bucket": customer_bucket, "prefix": "legal/knowledge-base"},
        )

    # No {kb_id}/raw/ suffix imposed — this is a path the customer already
    # owns and structures themselves.
    assert kb["s3_bucket"] == customer_bucket
    assert kb["s3_prefix"] == "legal/knowledge-base/"


async def test_sync_from_existing_s3_path_requires_bucket_in_source_config(
    make_user_and_token, fake_bedrock_agent: FakeBedrockAgentClient
) -> None:
    _, token = await make_user_and_token(TENANT_A, role="developer")

    with TestClient(app) as client:
        response = client.post(
            "/api/v1/platform/knowledge-bases",
            json={
                "name": "Missing Bucket KB",
                "description": "d",
                "source_type": "s3",
                "source_config": {"prefix": "legal/"},
            },
            headers=_bearer(token),
        )
    assert response.status_code == 422
    assert response.json()["detail"]["error"] == "invalid_source_config"
