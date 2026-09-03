"""Human-in-the-Loop pre-check + review creation (CLAUDE.md Section 38.7/38.8).

Writes directly to panasa-hitl-reviews (same {tenant_id, review_id} key
schema and item shape as app/modules/hitl/models.py's HitlReviewRecord) and
publishes directly to the configured SNS topic — not a call to the Factory
Runtime's own HITL API. That module's docstring anticipates a "future
Generated Agent Runtime process" reaching it either via its authenticated
HTTP API or "directly during development"; this runtime takes the direct
DynamoDB/SNS path deliberately, matching guardrail.py/rag_client.py's same
choice for the same reason (F8/R10 — zero runtime dependency on the
Factory Runtime API, not just on its Python package).

pre_check()'s trigger-condition matching is deliberately simple: a
case-insensitive substring match of each configured trigger_condition
against the incoming message. HumanReviewConfig.trigger_conditions are
free-form strings (e.g. "high_risk_decision") that were always meant to be
evaluated by something smarter (an LLM classifier, a rules engine) — that
richer evaluation is a real future improvement, not built here; this is a
correct, if blunt, first working version, not a final one.
"""

from __future__ import annotations

import asyncio
import os
import time
import uuid
from typing import Any

import boto3


class HITLManager:
    def __init__(
        self,
        agent_id: str,
        tenant_id: str,
        enabled: bool,
        trigger_conditions: list[str],
        timeout_hours: int = 24,
        notification_sns_arn: str | None = None,
        dynamodb: Any | None = None,
        sns_client: Any | None = None,
    ) -> None:
        self._agent_id = agent_id
        self._tenant_id = tenant_id
        self._enabled = enabled
        self._trigger_conditions = trigger_conditions
        self._timeout_hours = timeout_hours
        self._notification_sns_arn = notification_sns_arn
        region = os.environ.get("AWS_REGION", "eu-west-2")
        self._dynamodb = dynamodb or boto3.resource("dynamodb", region_name=region)
        self._sns = sns_client or boto3.client("sns", region_name=region)
        self._table_name = os.environ.get("DYNAMODB_HITL_REVIEWS_TABLE", "panasa-hitl-reviews")

    async def pre_check(self, message: str, context: str) -> dict[str, Any]:
        if not self._enabled or not self._trigger_conditions:
            return {"pause": False}
        lowered = message.lower()
        for condition in self._trigger_conditions:
            if condition.replace("_", " ").lower() in lowered:
                return {"pause": True, "trigger_condition": condition}
        return {"pause": False}

    async def create_review(
        self, run_id: str, agent_id: str, message: str, session_id: str, trigger_condition: str = "unspecified"
    ) -> str:
        review_id = f"HITL-{uuid.uuid4().hex[:8].upper()}"
        now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        item = {
            "tenant_id": self._tenant_id,
            "review_id": review_id,
            "agent_id": agent_id,
            "project_id": None,
            "trigger_condition": trigger_condition,
            "context_summary": f"run_id={run_id} session_id={session_id}\n{message}"[:2000],
            "status": "pending",
            "timeout_hours": self._timeout_hours,
            "requested_by": f"agent-runtime:{agent_id}",
            "requested_at": now,
        }
        table = self._dynamodb.Table(self._table_name)
        await asyncio.to_thread(table.put_item, Item=item)

        if self._notification_sns_arn:
            await asyncio.to_thread(
                self._sns.publish,
                TopicArn=self._notification_sns_arn,
                Subject=f"Human review requested — {agent_id}",
                Message=f"Review {review_id} ({trigger_condition}) is pending for agent {agent_id}.",
            )

        return review_id
