"""API tests for GET /api/v1/admin/audit-log (Priority 2 nav addition)."""

from __future__ import annotations

import contextlib
from collections.abc import Iterator

import boto3
import pytest
from fastapi.testclient import TestClient

from app.config import settings
from app.main import app

TENANT_A = "tenant-a"
AUDIT_BUCKET = "test-audit-log-api-bucket"


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture(autouse=True)
def _audit_bucket_configured(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    s3 = boto3.client("s3", region_name="eu-west-2")
    with contextlib.suppress(s3.exceptions.BucketAlreadyOwnedByYou):
        s3.create_bucket(
            Bucket=AUDIT_BUCKET, CreateBucketConfiguration={"LocationConstraint": "eu-west-2"}
        )
    monkeypatch.setattr(settings, "audit_s3_bucket", AUDIT_BUCKET)
    yield


async def test_audit_log_lists_events_created_via_agent_api(make_user_and_token) -> None:
    _, admin_token = await make_user_and_token(TENANT_A, role="admin")
    _, dev_token = await make_user_and_token(TENANT_A, role="developer")

    with TestClient(app) as client:
        # create_agent() writes a real config_change audit event.
        client.post(
            "/api/v1/agents",
            json={
                "name": "Audit Test Agent",
                "description": "d",
                "business_purpose": "b",
                "agent_type": "standard",
                "configuration": {
                    "model_id": "anthropic.claude-3-5-sonnet-20241022-v2:0",
                    "model_provider": "bedrock",
                    "system_prompt": "You are a test agent.",
                },
            },
            headers=_bearer(dev_token),
        )

        response = client.get("/api/v1/admin/audit-log", headers=_bearer(admin_token))

    assert response.status_code == 200
    items = response.json()["items"]
    assert any(item["event_type"] == "config_change" for item in items)


async def test_audit_log_forbidden_for_developer(make_user_and_token) -> None:
    _, token = await make_user_and_token(TENANT_A, role="developer")

    with TestClient(app) as client:
        response = client.get("/api/v1/admin/audit-log", headers=_bearer(token))

    assert response.status_code == 403


async def test_audit_log_defaults_to_last_7_days(make_user_and_token) -> None:
    _, token = await make_user_and_token(TENANT_A, role="admin")

    with TestClient(app) as client:
        response = client.get("/api/v1/admin/audit-log", headers=_bearer(token))

    assert response.status_code == 200
    assert response.json() == {"items": []}
