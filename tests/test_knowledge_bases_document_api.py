"""API tests for the Knowledge Base document upload/list/delete and sync
trigger/status endpoints (instructions_kb_api.md / CLAUDE.md Section 43).

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


def _ensure_bucket() -> None:
    s3 = boto3.client("s3", region_name="eu-west-2")
    with contextlib.suppress(s3.exceptions.BucketAlreadyOwnedByYou):
        s3.create_bucket(
            Bucket=TEST_KB_BUCKET, CreateBucketConfiguration={"LocationConstraint": "eu-west-2"}
        )


@pytest.fixture(autouse=True)
def _kb_bucket_configured(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
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
    client: TestClient, token: str, name: str = "Payroll Policy Documents"
) -> dict[str, Any]:
    response = client.post(
        "/api/v1/platform/knowledge-bases",
        json={"name": name, "description": "Compliance docs", "source_type": "manual"},
        headers=_bearer(token),
    )
    assert response.status_code == 201
    return dict(response.json())


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
    assert kb["s3_prefix"] == f"{TENANT_A}/{kb['kb_id']}/raw/"
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
        assert body["uploaded"][0]["s3_key"] == f"{TENANT_A}/{kb_id}/raw/policy.pdf"

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
        client.post(
            f"/api/v1/platform/knowledge-bases/{kb_id}/documents",
            files={"files": ("policy.pdf", b"content", "application/pdf")},
            headers=_bearer(token),
        )

        deleted = client.delete(
            f"/api/v1/platform/knowledge-bases/{kb_id}", headers=_bearer(token)
        )
        assert deleted.status_code == 204

    assert len(fake_bedrock_agent.delete_ds_calls) == 1
    assert len(fake_bedrock_agent.delete_kb_calls) == 1
