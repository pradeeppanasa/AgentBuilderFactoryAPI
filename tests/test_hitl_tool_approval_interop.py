"""Cross-service interop check (CLAUDE.md F8 boundary, Sprint 4 Phase 3 —
S-11).

Mirrors services/agent-runtime/tests/test_tool_executor.py's
test_lambda_name_matches_terraform_naming_convention in spirit, in the
opposite direction: the two services share zero code, so nothing else
would catch services/agent-runtime's ToolApprovalManager (which writes
directly to panasa-hitl-reviews via its own boto3 calls) silently
drifting out of sync with this Runtime's own strict (extra="forbid")
HitlReviewRecord. This test imports the REAL ToolApprovalManager, has it
build a REAL item dict, and parses that dict through the REAL model —
not a hand-maintained copy of either side's field list.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any

from app.modules.hitl.models import HitlReviewRecord, HitlReviewStatus

_REPO_ROOT = Path(__file__).resolve().parents[1]
_AGENT_RUNTIME_ROOT = _REPO_ROOT / "services" / "agent-runtime"


class _FakeTable:
    def __init__(self) -> None:
        self.items: dict[tuple[str, str], dict[str, Any]] = {}

    def put_item(self, Item: dict[str, Any]) -> dict[str, Any]:
        self.items[(Item["tenant_id"], Item["review_id"])] = Item
        return {}

    def update_item(
        self,
        Key: dict[str, Any],
        UpdateExpression: str,
        ExpressionAttributeValues: dict[str, Any],
        ConditionExpression: str | None = None,
    ) -> dict[str, Any]:
        item = self.items[(Key["tenant_id"], Key["review_id"])]
        attr, placeholder = (
            part.strip() for part in UpdateExpression.removeprefix("SET").split("=")
        )
        item[attr] = ExpressionAttributeValues[placeholder]
        return {}


class _ConditionalCheckFailedException(Exception):
    pass


class _FakeDynamoDBResource:
    """Minimal enough for ToolApprovalManager's own calls — not a general
    DynamoDB fake (that's services/agent-runtime/tests/fakes.py's job,
    which this Factory Runtime test deliberately does not import, to keep
    this interop check to production code only, per the module docstring)."""

    def __init__(self, table: _FakeTable) -> None:
        self._table = table

        class _Exceptions:
            ConditionalCheckFailedException = _ConditionalCheckFailedException

        class _Client:
            exceptions = _Exceptions()

        class _Meta:
            client = _Client()

        self.meta = _Meta()

    def Table(self, name: str) -> _FakeTable:
        return self._table


def test_tool_approval_manager_item_parses_through_hitl_review_record() -> None:
    sys.path.insert(0, str(_AGENT_RUNTIME_ROOT))
    try:
        from tool_approval import ToolApprovalManager
    finally:
        sys.path.remove(str(_AGENT_RUNTIME_ROOT))

    table = _FakeTable()
    manager = ToolApprovalManager(
        agent_id="agent-1",
        tenant_id="tenant-a",
        dynamodb=_FakeDynamoDBResource(table),
        sns_client=None,
    )

    review_id = asyncio.run(
        manager.request_approval(
            tool_id="db-delete",
            run_id="run-1",
            session_id="s1",
            resume_context='{"tool_id":"db-delete"}',
        )
    )

    raw_item = table.items[("tenant-a", review_id)]
    record = HitlReviewRecord(**raw_item)
    assert record.kind == "tool_approval"
    assert record.tool_id == "db-delete"
    assert record.status == HitlReviewStatus.PENDING

    # Simulate the Factory Runtime's own approve() overwriting the WHOLE
    # item via model_dump() (app/modules/hitl/store.py's _decide()) —
    # resumed_at=None must still round-trip and still be claimable.
    approved = record.model_copy(update={"status": HitlReviewStatus.APPROVED})
    table.put_item(Item=approved.model_dump(mode="json"))

    claimed = asyncio.run(manager.try_claim_resume(review_id))
    assert claimed is True

    # And the item is still valid after ToolApprovalManager's own write.
    final_record = HitlReviewRecord(**table.items[("tenant-a", review_id)])
    assert final_record.resumed_at is not None
