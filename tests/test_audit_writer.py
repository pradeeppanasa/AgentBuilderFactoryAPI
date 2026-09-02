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


# ── list_events (Audit Log page, Priority 2) ────────────────────────────


async def test_list_events_returns_events_in_date_range(s3_client: Any) -> None:
    stub_settings = settings.model_copy(update={"audit_s3_bucket": "test-audit-bucket"})
    writer = AuditWriter(s3_client, stub_settings)

    today = datetime.now(UTC).date().isoformat()
    await writer.write(_event(summary="In range", occurred_at=f"{today}T10:00:00+00:00"))
    await writer.write(
        _event(summary="Out of range", occurred_at="2020-01-01T10:00:00+00:00")
    )

    events = await writer.list_events(tenant_id="tenant-a", date_from=today, date_to=today)

    assert len(events) == 1
    assert events[0].summary == "In range"


async def test_list_events_filters_by_actor_and_agent(s3_client: Any) -> None:
    stub_settings = settings.model_copy(update={"audit_s3_bucket": "test-audit-bucket"})
    writer = AuditWriter(s3_client, stub_settings)

    today = datetime.now(UTC).date().isoformat()
    await writer.write(
        _event(actor="alice@example.com", agent_id="agent-1", occurred_at=f"{today}T09:00:00+00:00")
    )
    await writer.write(
        _event(actor="bob@example.com", agent_id="agent-2", occurred_at=f"{today}T09:01:00+00:00")
    )

    by_actor = await writer.list_events(
        tenant_id="tenant-a", date_from=today, date_to=today, actor="alice@example.com"
    )
    assert len(by_actor) == 1
    assert by_actor[0].actor == "alice@example.com"

    by_agent = await writer.list_events(
        tenant_id="tenant-a", date_from=today, date_to=today, agent_id="agent-2"
    )
    assert len(by_agent) == 1
    assert by_agent[0].agent_id == "agent-2"


async def test_list_events_scoped_to_tenant(s3_client: Any) -> None:
    stub_settings = settings.model_copy(update={"audit_s3_bucket": "test-audit-bucket"})
    writer = AuditWriter(s3_client, stub_settings)

    today = datetime.now(UTC).date().isoformat()
    await writer.write(_event(tenant_id="tenant-a", occurred_at=f"{today}T09:00:00+00:00"))
    await writer.write(_event(tenant_id="tenant-b", occurred_at=f"{today}T09:00:00+00:00"))

    events = await writer.list_events(tenant_id="tenant-a", date_from=today, date_to=today)

    assert len(events) == 1
    assert events[0].tenant_id == "tenant-a"


async def test_list_events_returns_empty_when_bucket_not_configured() -> None:
    stub_settings = settings.model_copy(update={"audit_s3_bucket": None})
    writer = AuditWriter(None, stub_settings)

    events = await writer.list_events(tenant_id="tenant-a", date_from="2020-01-01", date_to="2020-01-01")

    assert events == []
