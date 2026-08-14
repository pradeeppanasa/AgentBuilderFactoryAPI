"""Unit tests for app.modules.audit.writer (CLAUDE.md Section 14)."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

import boto3
import pytest

from app.config import settings
from app.modules.audit.writer import AuditEvent, AuditWriter


@pytest.fixture
def s3_client() -> Any:
    client = boto3.client("s3", region_name="eu-west-2")
    client.create_bucket(
        Bucket="test-audit-bucket",
        CreateBucketConfiguration={"LocationConstraint": "eu-west-2"},
    )
    return client


def _event(**overrides: Any) -> AuditEvent:
    data: dict[str, Any] = {
        "event_type": "config_change",
        "tenant_id": "tenant-a",
        "agent_id": "agent-1",
        "actor": "dev@example.com",
        "summary": "Agent created",
        "occurred_at": datetime.now(UTC).isoformat(),
    }
    data.update(overrides)
    return AuditEvent(**data)


async def test_write_persists_event_under_expected_key(s3_client: Any) -> None:
    stub_settings = settings.model_copy(update={"audit_s3_bucket": "test-audit-bucket"})
    writer = AuditWriter(s3_client, stub_settings)

    key = await writer.write(_event())

    assert key is not None
    assert key.startswith("audit/tenant-a/config_change/")
    assert key.endswith(".json")

    body = s3_client.get_object(Bucket="test-audit-bucket", Key=key)["Body"].read()
    parsed = json.loads(body)
    assert parsed["tenant_id"] == "tenant-a"
    assert parsed["event_type"] == "config_change"
    assert parsed["summary"] == "Agent created"


async def test_write_returns_none_when_bucket_not_configured(s3_client: Any) -> None:
    stub_settings = settings.model_copy(update={"audit_s3_bucket": None})
    writer = AuditWriter(s3_client, stub_settings)

    assert await writer.write(_event()) is None


async def test_write_returns_none_on_s3_failure() -> None:
    class _ExplodingClient:
        def put_object(self, **kwargs: Any) -> None:
            raise RuntimeError("S3 is down")

    stub_settings = settings.model_copy(update={"audit_s3_bucket": "test-audit-bucket"})
    writer = AuditWriter(_ExplodingClient(), stub_settings)

    assert await writer.write(_event()) is None


async def test_each_event_type_produces_a_distinct_key_prefix(s3_client: Any) -> None:
    stub_settings = settings.model_copy(update={"audit_s3_bucket": "test-audit-bucket"})
    writer = AuditWriter(s3_client, stub_settings)

    for event_type in ["config_change", "deploy", "rollback", "block"]:
        key = await writer.write(_event(event_type=event_type))
        assert key is not None
        assert f"/{event_type}/" in key
