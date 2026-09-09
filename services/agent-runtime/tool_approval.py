"""Tool-call human approval queue (Sprint 4 Phase 3 — CLAUDE.md Section
64.3/R64, S-11).

Persists a HIGH/DESTRUCTIVE-risk tool call that ToolPolicyEngine.enforce()
blocked pending human approval, and lets the paused conversation turn be
resumed once a decision is made. Writes directly to the same
panasa-hitl-reviews table hitl.py's HITLManager already uses (see
app/modules/hitl/models.py's docstring, kind="tool_approval") — not a new
table, and not a call to the Factory Runtime's HTTP API: F8/R10 forbid
this Runtime from depending on the Factory Runtime at all post-deploy,
the same reasoning hitl.py already documents for its own direct
DynamoDB/SNS writes.

The approve/reject decision itself is made through the Factory Runtime's
EXISTING POST /api/v1/hitl/reviews/{review_id}/approve|reject endpoints
(app/api/v1/hitl.py) — nothing new is built there. This module only needs
read access to see the resulting status, plus enough persisted context
(`resume_context`, a JSON string) to re-invoke the tool and continue the
LLM round-trip once approved.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import boto3

_DEFAULT_TIMEOUT_HOURS = 24


@dataclass
class ApprovalDecision:
    status: str  # "pending" | "approved" | "rejected" | "timeout" | "not_found"
    review_id: str
    resume_context: str | None


class ToolApprovalManager:
    def __init__(
        self,
        agent_id: str,
        tenant_id: str,
        notification_sns_arn: str | None = None,
        timeout_hours: int = _DEFAULT_TIMEOUT_HOURS,
        dynamodb: Any | None = None,
        sns_client: Any | None = None,
    ) -> None:
        self._agent_id = agent_id
        self._tenant_id = tenant_id
        self._notification_sns_arn = notification_sns_arn
        self._timeout_hours = timeout_hours
        region = os.environ.get("AWS_REGION", "eu-west-2")
        self._dynamodb = dynamodb or boto3.resource("dynamodb", region_name=region)
        self._sns = sns_client or boto3.client("sns", region_name=region)
        self._table_name = os.environ.get("DYNAMODB_HITL_REVIEWS_TABLE", "panasa-hitl-reviews")

    async def request_approval(
        self, tool_id: str, run_id: str, session_id: str, resume_context: str
    ) -> str:
        review_id = f"TAPR-{uuid.uuid4().hex[:8].upper()}"
        now = datetime.now(UTC).isoformat()
        item = {
            "tenant_id": self._tenant_id,
            "review_id": review_id,
            "agent_id": self._agent_id,
            "project_id": None,
            "trigger_condition": f"tool_risk:{tool_id}",
            "context_summary": f"run_id={run_id} session_id={session_id} tool={tool_id}",
            "status": "pending",
            "timeout_hours": self._timeout_hours,
            "requested_by": f"agent-runtime:{self._agent_id}",
            "requested_at": now,
            "kind": "tool_approval",
            "tool_id": tool_id,
            "resume_context": resume_context,
        }
        table = self._dynamodb.Table(self._table_name)
        await asyncio.to_thread(table.put_item, Item=item)

        if self._notification_sns_arn:
            await asyncio.to_thread(
                self._sns.publish,
                TopicArn=self._notification_sns_arn,
                Subject=f"Tool approval requested — {self._agent_id}",
                Message=(
                    f"Review {review_id}: tool {tool_id!r} requires approval "
                    f"for agent {self._agent_id} (session {session_id})."
                ),
            )
        return review_id

    async def try_claim_resume(self, review_id: str) -> bool:
        """Atomically marks this review as resumed. Returns True if THIS
        call is the one that claimed it (safe to execute the tool now /
        finalize the outcome); False if another resume call already
        claimed it. Prevents a racing or retried resume call from
        invoking a DESTRUCTIVE tool a second time — the one thing this
        module cannot afford to get wrong. Unlike request_approval()'s
        plain put_item, this MUST be a conditional update.

        The condition accepts both "attribute missing" AND "attribute
        present but NULL type" as claimable: the Factory Runtime's own
        approve()/reject() (app/modules/hitl/store.py's _decide())
        rewrites the WHOLE item via HitlReviewRecord.model_dump(), which
        serialises resumed_at=None as an explicit DynamoDB NULL — not an
        absent attribute — the moment a human approves/rejects, well
        before any resume call happens. Checking attribute_not_exists()
        alone would make the very first approve/reject call defeat this
        guard permanently.
        """
        table = self._dynamodb.Table(self._table_name)
        try:
            await asyncio.to_thread(
                table.update_item,
                Key={"tenant_id": self._tenant_id, "review_id": review_id},
                UpdateExpression="SET resumed_at = :now",
                ConditionExpression=(
                    "attribute_not_exists(resumed_at) OR attribute_type(resumed_at, :null_type)"
                ),
                ExpressionAttributeValues={
                    ":now": datetime.now(UTC).isoformat(),
                    ":null_type": "NULL",
                },
            )
            return True
        except self._dynamodb.meta.client.exceptions.ConditionalCheckFailedException:
            return False

    async def get_decision(self, review_id: str) -> ApprovalDecision:
        table = self._dynamodb.Table(self._table_name)
        response = await asyncio.to_thread(
            table.get_item, Key={"tenant_id": self._tenant_id, "review_id": review_id}
        )
        item = response.get("Item")
        if item is None:
            return ApprovalDecision(status="not_found", review_id=review_id, resume_context=None)

        status = str(item.get("status", "pending"))
        resume_context = item.get("resume_context")

        if status == "pending":
            requested_at = item.get("requested_at")
            timeout_hours = float(item.get("timeout_hours", self._timeout_hours))
            if requested_at and self._is_expired(str(requested_at), timeout_hours):
                return ApprovalDecision(
                    status="timeout", review_id=review_id, resume_context=resume_context
                )

        return ApprovalDecision(status=status, review_id=review_id, resume_context=resume_context)

    @staticmethod
    def _is_expired(requested_at: str, timeout_hours: float) -> bool:
        try:
            requested = datetime.fromisoformat(requested_at)
        except ValueError:
            return False
        if requested.tzinfo is None:
            requested = requested.replace(tzinfo=UTC)
        return datetime.now(UTC) > requested + timedelta(hours=timeout_hours)
