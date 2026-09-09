from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta

from tool_approval import ToolApprovalManager

from fakes import FakeDynamoDBResource, FakeSNSClient, FakeTable


def _manager(**overrides: object) -> tuple[ToolApprovalManager, FakeTable, FakeSNSClient]:
    table = FakeTable(key_names=("tenant_id", "review_id"))
    dynamodb = FakeDynamoDBResource({"panasa-hitl-reviews": table})
    sns = FakeSNSClient()
    defaults: dict[str, object] = dict(
        agent_id="agent-1", tenant_id="tenant-a", dynamodb=dynamodb, sns_client=sns
    )
    defaults.update(overrides)
    return ToolApprovalManager(**defaults), table, sns  # type: ignore[arg-type]


async def test_request_approval_writes_tool_approval_kind_and_publishes_sns() -> None:
    manager, table, sns = _manager(notification_sns_arn="arn:aws:sns:eu-west-2:111122223333:topic")

    review_id = await manager.request_approval(
        tool_id="db-delete",
        run_id="run-1",
        session_id="s1",
        resume_context='{"tool_id":"db-delete"}',
    )

    assert review_id.startswith("TAPR-")
    assert len(table.put_calls) == 1
    saved = table.put_calls[0]
    assert saved["kind"] == "tool_approval"
    assert saved["tool_id"] == "db-delete"
    assert saved["status"] == "pending"
    assert saved["resume_context"] == '{"tool_id":"db-delete"}'
    assert "resumed_at" not in saved

    assert len(sns.published) == 1
    assert "db-delete" in sns.published[0]["Message"]


async def test_request_approval_without_sns_arn_skips_publish() -> None:
    manager, _table, sns = _manager(notification_sns_arn=None)

    await manager.request_approval(
        tool_id="db-delete", run_id="run-1", session_id="s1", resume_context="{}"
    )

    assert sns.published == []


async def test_get_decision_not_found() -> None:
    manager, _table, _sns = _manager()

    decision = await manager.get_decision("TAPR-missing")

    assert decision.status == "not_found"
    assert decision.resume_context is None


async def test_get_decision_pending_within_timeout_window() -> None:
    manager, table, _sns = _manager()
    table.items[("tenant-a", "TAPR-1")] = {
        "tenant_id": "tenant-a",
        "review_id": "TAPR-1",
        "status": "pending",
        "requested_at": datetime.now(UTC).isoformat(),
        "timeout_hours": 24,
        "resume_context": '{"tool_id":"db-delete"}',
    }

    decision = await manager.get_decision("TAPR-1")

    assert decision.status == "pending"
    assert decision.resume_context == '{"tool_id":"db-delete"}'


async def test_get_decision_pending_past_timeout_window_reports_timeout() -> None:
    manager, table, _sns = _manager()
    table.items[("tenant-a", "TAPR-1")] = {
        "tenant_id": "tenant-a",
        "review_id": "TAPR-1",
        "status": "pending",
        "requested_at": (datetime.now(UTC) - timedelta(hours=25)).isoformat(),
        "timeout_hours": 24,
        "resume_context": "{}",
    }

    decision = await manager.get_decision("TAPR-1")

    assert decision.status == "timeout"


async def test_get_decision_passes_through_approved_and_rejected_status() -> None:
    manager, table, _sns = _manager()
    table.items[("tenant-a", "TAPR-1")] = {
        "tenant_id": "tenant-a",
        "review_id": "TAPR-1",
        "status": "approved",
        "requested_at": datetime.now(UTC).isoformat(),
        "timeout_hours": 24,
        "resume_context": "{}",
    }

    decision = await manager.get_decision("TAPR-1")

    assert decision.status == "approved"


async def test_try_claim_resume_succeeds_when_never_claimed() -> None:
    manager, table, _sns = _manager()
    table.items[("tenant-a", "TAPR-1")] = {"tenant_id": "tenant-a", "review_id": "TAPR-1"}

    claimed = await manager.try_claim_resume("TAPR-1")

    assert claimed is True
    assert table.items[("tenant-a", "TAPR-1")]["resumed_at"] is not None


async def test_try_claim_resume_succeeds_when_resumed_at_is_an_explicit_null() -> None:
    """Regression guard: HitlReviewStore._decide() (the Factory Runtime's
    approve()/reject()) rewrites the WHOLE item via HitlReviewRecord.
    model_dump(), which serialises resumed_at=None as an explicit
    DynamoDB NULL the moment a human approves/rejects — well before any
    resume call happens. That must still count as claimable."""
    manager, table, _sns = _manager()
    table.items[("tenant-a", "TAPR-1")] = {
        "tenant_id": "tenant-a",
        "review_id": "TAPR-1",
        "resumed_at": None,
    }

    claimed = await manager.try_claim_resume("TAPR-1")

    assert claimed is True


async def test_try_claim_resume_fails_once_already_claimed() -> None:
    manager, table, _sns = _manager()
    table.items[("tenant-a", "TAPR-1")] = {
        "tenant_id": "tenant-a",
        "review_id": "TAPR-1",
        "resumed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }

    claimed = await manager.try_claim_resume("TAPR-1")

    assert claimed is False
