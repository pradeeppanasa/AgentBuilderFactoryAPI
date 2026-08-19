"""Run store — DynamoDB CRUD (Observability — Runs Feature, Phase 1).

Keyed by agent_id (HASH) / started_at (RANGE, ISO 8601 — lexicographically
sortable, so a plain Query with ScanIndexForward=False returns newest-first
with no separate index) — same "keyed by agent_id, not tenant_id" shape as
app.modules.playground.store.PlaygroundSessionStore, since a run is always
looked up in the context of one specific agent; R01 tenant scoping is
enforced by every route requiring a tenant-checked
AgentRegistryStore.get_agent() before ever reaching this store, and
tenant_id is still carried as a plain attribute for audit purposes.

`run_id` (the short "RUN-XXXXXXXX" shown in the UI) is a separate attribute
from the sort key — looking one up means a bounded Query + filter within
the agent's own partition, not a direct get_item. Fine at the scale a single
agent's run history actually reaches; revisit with a GSI if that changes.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from boto3.dynamodb.conditions import Attr, Key
from botocore.exceptions import ClientError

from app.config import Settings
from app.modules.runs.errors import error_category, map_error
from app.modules.runs.models import (
    ActivityEvent,
    LogLine,
    RagDocument,
    RagRetrievalDetail,
    RunRecord,
    RunStatus,
    RunStep,
    RunSummary,
    RunTrigger,
    Span,
    StepError,
)
from app.shared.dynamodb_types import decimal_to_native, native_to_decimal


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _new_run_id() -> str:
    return f"RUN-{uuid.uuid4().hex[:8].upper()}"


def _new_step_id() -> str:
    return f"step-{uuid.uuid4().hex[:8]}"


class RunNotFoundError(Exception):
    def __init__(self, run_id: str) -> None:
        self.run_id = run_id
        super().__init__(f"Run {run_id!r} not found")


class RunStore:
    def __init__(self, dynamodb_resource: Any, settings: Settings) -> None:
        self._dynamodb = dynamodb_resource
        self._settings = settings
        self._table = dynamodb_resource.Table(settings.dynamodb_runs_table)

    async def ensure_table(self) -> None:
        def _create() -> None:
            try:
                table = self._dynamodb.create_table(
                    TableName=self._settings.dynamodb_runs_table,
                    KeySchema=[
                        {"AttributeName": "agent_id", "KeyType": "HASH"},
                        {"AttributeName": "started_at", "KeyType": "RANGE"},
                    ],
                    AttributeDefinitions=[
                        {"AttributeName": "agent_id", "AttributeType": "S"},
                        {"AttributeName": "started_at", "AttributeType": "S"},
                    ],
                    BillingMode="PAY_PER_REQUEST",
                )
                table.wait_until_exists()
            except ClientError as exc:
                if exc.response["Error"]["Code"] != "ResourceInUseException":
                    raise

        await asyncio.to_thread(_create)

    async def list_runs(
        self,
        tenant_id: str,
        agent_id: str,
        status: RunStatus | None = None,
        trigger: RunTrigger | None = None,
        version: int | None = None,
        since_iso: str | None = None,
        limit: int = 50,
    ) -> list[RunRecord]:
        filters = None
        if status is not None:
            filters = Attr("status").eq(status)
        if trigger is not None:
            clause = Attr("trigger").eq(trigger)
            filters = clause if filters is None else filters & clause
        if version is not None:
            clause = Attr("version").eq(version)
            filters = clause if filters is None else filters & clause

        key_condition = Key("agent_id").eq(agent_id)
        if since_iso is not None:
            key_condition = key_condition & Key("started_at").gte(since_iso)

        query_kwargs: dict[str, Any] = {
            "KeyConditionExpression": key_condition,
            "ScanIndexForward": False,
            "Limit": limit,
        }
        if filters is not None:
            query_kwargs["FilterExpression"] = filters

        response = await asyncio.to_thread(self._table.query, **query_kwargs)
        return [
            RunRecord(**decimal_to_native(item))
            for item in response.get("Items", [])
            if item.get("tenant_id") == tenant_id
        ]

    async def _list_all_runs_since(
        self, tenant_id: str, agent_id: str, since_iso: str
    ) -> list[RunRecord]:
        """Paginates through every run since `since_iso` — Section 10's
        7-day summary needs a true aggregate, not just the first page a
        plain `limit` would return."""
        records: list[RunRecord] = []
        exclusive_start_key: dict[str, Any] | None = None
        while True:
            query_kwargs: dict[str, Any] = {
                "KeyConditionExpression": (
                    Key("agent_id").eq(agent_id) & Key("started_at").gte(since_iso)
                ),
                "ScanIndexForward": False,
            }
            if exclusive_start_key is not None:
                query_kwargs["ExclusiveStartKey"] = exclusive_start_key
            response = await asyncio.to_thread(self._table.query, **query_kwargs)
            records.extend(
                RunRecord(**decimal_to_native(item))
                for item in response.get("Items", [])
                if item.get("tenant_id") == tenant_id
            )
            exclusive_start_key = response.get("LastEvaluatedKey")
            if exclusive_start_key is None:
                break
        return records

    async def get_summary(self, tenant_id: str, agent_id: str, window_days: int) -> RunSummary:
        since_iso = (datetime.now(UTC) - timedelta(days=window_days)).isoformat()
        runs = await self._list_all_runs_since(tenant_id, agent_id, since_iso)

        completed = [r for r in runs if r.duration_ms is not None]
        errors = [r for r in runs if r.status == "FAILED"]
        total_tokens = sum(
            (step.input_tokens or 0) + (step.output_tokens or 0)
            for run in runs
            for step in run.steps
        )
        total_cost = sum(r.cost_usd or 0.0 for r in runs)

        return RunSummary(
            window_days=window_days,
            total_runs=len(runs),
            success_rate=(
                sum(1 for r in runs if r.status == "SUCCESS") / len(runs)
                if runs
                else None
            ),
            avg_latency_ms=(
                sum(r.duration_ms for r in completed if r.duration_ms is not None)
                / len(completed)
                if completed
                else None
            ),
            error_count=len(errors),
            total_tokens=total_tokens,
            estimated_cost_usd=round(total_cost, 4),
        )

    async def get_logs(
        self, tenant_id: str, agent_id: str, run_id: str
    ) -> list[LogLine] | None:
        """Section 8 — Logs tab. Reuses the run's own (already-sanitised)
        ActivityEvent feed as the log source; see LogLine's docstring for
        why there's no real CloudWatch proxy behind this yet."""
        run = await self.get_run(tenant_id, agent_id, run_id)
        if run is None:
            return None
        return [
            LogLine(timestamp=event.occurred_at, level=event.level, message=event.message)
            for event in run.activity
        ]

    async def get_run(self, tenant_id: str, agent_id: str, run_id: str) -> RunRecord | None:
        response = await asyncio.to_thread(
            self._table.query,
            KeyConditionExpression=Key("agent_id").eq(agent_id),
            FilterExpression=Attr("run_id").eq(run_id) & Attr("tenant_id").eq(tenant_id),
        )
        items = response.get("Items", [])
        if not items:
            return None
        return RunRecord(**decimal_to_native(items[0]))

    async def _put(self, record: RunRecord) -> None:
        item = native_to_decimal(record.model_dump(mode="json"))
        await asyncio.to_thread(self._table.put_item, Item=item)

    async def seed_demo_runs(self, tenant_id: str, agent_id: str, version: int) -> list[RunRecord]:
        """Dev/test-only synthetic data (gated behind settings.seed_runs_enabled
        — see app/api/v1/runs.py) so the Runs list/detail/filters can be
        exercised without a real Generated Agent Runtime emitting telemetry,
        which doesn't exist in this Stage 1 environment. One of each of the
        four states the doc's re-test checklist calls for: success, failed,
        running, scheduler-triggered. Rendered with no "test data" marker —
        indistinguishable in the UI from a real run, by design."""
        now = datetime.now(UTC)

        def _iso(seconds_ago: int) -> str:
            return (now - timedelta(seconds=seconds_ago)).isoformat()

        auth_reason, auth_action = map_error("UnrecognizedClientException")

        success = RunRecord(
            run_id=_new_run_id(),
            agent_id=agent_id,
            tenant_id=tenant_id,
            version=version,
            status="SUCCESS",
            trigger="API",
            started_at=_iso(180),
            duration_ms=4800,
            cost_usd=0.03,
            is_seed_data=True,
            activity=[
                ActivityEvent(level="INFO", message="Request received", occurred_at=_iso(180)),
                ActivityEvent(
                    level="INFO",
                    message="Guardrail check passed",
                    occurred_at=_iso(180),
                    elapsed_ms=42,
                ),
                ActivityEvent(
                    level="INFO",
                    message="Orchestrator routing completed",
                    occurred_at=_iso(179),
                    elapsed_ms=180,
                ),
                ActivityEvent(
                    level="INFO",
                    message="Knowledge base retrieval completed",
                    occurred_at=_iso(179),
                    elapsed_ms=620,
                ),
                ActivityEvent(
                    level="INFO",
                    message="LLM call completed — 1,245 in / 318 out",
                    occurred_at=_iso(177),
                    elapsed_ms=2900,
                ),
                ActivityEvent(
                    level="INFO",
                    message="Tool call completed — HTTP 200",
                    occurred_at=_iso(176),
                    elapsed_ms=3740,
                ),
                ActivityEvent(
                    level="INFO",
                    message="Response returned to caller",
                    occurred_at=_iso(175),
                    elapsed_ms=4800,
                ),
            ],
            steps=[
                RunStep(
                    step_id=_new_step_id(),
                    name="Guardrail check",
                    component="Guardrail Engine",
                    status="SUCCESS",
                    start_offset_ms=0,
                    duration_ms=42,
                ),
                RunStep(
                    step_id=_new_step_id(),
                    name="Orchestrator routing",
                    component="Orchestrator",
                    status="SUCCESS",
                    start_offset_ms=42,
                    duration_ms=180,
                ),
                RunStep(
                    step_id=_new_step_id(),
                    name="Knowledge Base",
                    component="Knowledge Base",
                    status="SUCCESS",
                    start_offset_ms=222,
                    duration_ms=620,
                    rag=RagRetrievalDetail(
                        documents_returned=5,
                        relevant_count=4,
                        retrieval_latency_ms=620,
                        documents=[
                            RagDocument(label="Document 1", relevance=0.94),
                            RagDocument(label="Document 2", relevance=0.91),
                            RagDocument(label="Document 3", relevance=0.87),
                            RagDocument(label="Document 4", relevance=0.81),
                            RagDocument(label="Document 5", relevance=0.42),
                        ],
                    ),
                ),
                RunStep(
                    step_id=_new_step_id(),
                    name="Claude 3.5 Sonnet",
                    component="Amazon Bedrock",
                    status="SUCCESS",
                    start_offset_ms=842,
                    duration_ms=2900,
                    model_id="claude-3-5-sonnet-20241022",
                    input_tokens=1245,
                    output_tokens=318,
                    cost_usd=0.014,
                    retry_count=0,
                ),
                RunStep(
                    step_id=_new_step_id(),
                    name="Payroll API tool",
                    component="Tool: payroll_api",
                    status="SUCCESS",
                    start_offset_ms=3742,
                    duration_ms=840,
                    retry_count=0,
                ),
                RunStep(
                    step_id=_new_step_id(),
                    name="Response assembly",
                    component="Agent Runtime",
                    status="SUCCESS",
                    start_offset_ms=4582,
                    duration_ms=210,
                ),
            ],
            spans=[
                Span(
                    span_id="span-root",
                    parent_span_id=None,
                    name=f"{agent_id}",
                    start_offset_ms=0,
                    duration_ms=4800,
                    status="SUCCESS",
                    tags={"agent_id": agent_id, "version": str(version), "trigger": "API"},
                ),
                Span(
                    span_id="span-orchestration",
                    parent_span_id="span-root",
                    name="Agent Orchestration",
                    start_offset_ms=42,
                    duration_ms=4758,
                    status="SUCCESS",
                ),
                Span(
                    span_id="span-guardrail",
                    parent_span_id="span-orchestration",
                    name="Guardrail Check",
                    start_offset_ms=0,
                    duration_ms=42,
                    status="SUCCESS",
                ),
                Span(
                    span_id="span-llm",
                    parent_span_id="span-orchestration",
                    name="Generate AI Response",
                    start_offset_ms=842,
                    duration_ms=2900,
                    status="SUCCESS",
                    attributes={
                        "model_id": "claude-3-5-sonnet-20241022",
                        "token_count_input": "1245",
                        "token_count_output": "318",
                    },
                ),
                Span(
                    span_id="span-tool-dispatch",
                    parent_span_id="span-orchestration",
                    name="Tool Dispatch",
                    start_offset_ms=3742,
                    duration_ms=840,
                    status="SUCCESS",
                ),
                Span(
                    span_id="span-payroll-tool",
                    parent_span_id="span-tool-dispatch",
                    name="payroll_api",
                    start_offset_ms=0,
                    duration_ms=840,
                    status="SUCCESS",
                    attributes={"status_code": "200"},
                ),
            ],
            ragas_scores={
                "faithfulness": 0.94,
                "answer_relevance": 0.91,
                "context_precision": 0.89,
                "context_recall": 0.86,
                "context_relevance": 0.92,
            },
        )

        failed = RunRecord(
            run_id=_new_run_id(),
            agent_id=agent_id,
            tenant_id=tenant_id,
            version=version,
            status="FAILED",
            trigger="MANUAL",
            started_at=_iso(600),
            duration_ms=1200,
            cost_usd=0.0,
            is_seed_data=True,
            error_category=error_category("UnrecognizedClientException"),
            activity=[
                ActivityEvent(level="INFO", message="Request received", occurred_at=_iso(600)),
                ActivityEvent(
                    level="INFO",
                    message="Guardrail check passed",
                    occurred_at=_iso(600),
                    elapsed_ms=38,
                ),
                ActivityEvent(
                    level="ERROR",
                    message=(
                        "Model invocation failed — AWS credentials are invalid or the model "
                        "is not enabled in this region"
                    ),
                    occurred_at=_iso(599),
                    elapsed_ms=1200,
                ),
            ],
            steps=[
                RunStep(
                    step_id=_new_step_id(),
                    name="Guardrail check",
                    component="Guardrail Engine",
                    status="SUCCESS",
                    start_offset_ms=0,
                    duration_ms=38,
                ),
                RunStep(
                    step_id=_new_step_id(),
                    name="Model invocation",
                    component="Amazon Bedrock",
                    status="FAILED",
                    start_offset_ms=38,
                    duration_ms=1162,
                    model_id="claude-3-5-sonnet-20241022",
                    retry_count=0,
                    error=StepError(
                        business_reason=auth_reason,
                        recommended_action=auth_action,
                        raw_error_code="UnrecognizedClientException",
                        request_id="req_abc123",
                        trace_id="tr_def456",
                        region="eu-west-1",
                        occurred_at=_iso(599),
                    ),
                ),
            ],
        )

        running = RunRecord(
            run_id=_new_run_id(),
            agent_id=agent_id,
            tenant_id=tenant_id,
            version=version,
            status="RUNNING",
            trigger="HITL",
            started_at=_iso(3),
            duration_ms=None,
            cost_usd=None,
            is_seed_data=True,
            activity=[
                ActivityEvent(level="INFO", message="Request received", occurred_at=_iso(3)),
                ActivityEvent(
                    level="INFO",
                    message="Guardrail check passed",
                    occurred_at=_iso(3),
                    elapsed_ms=40,
                ),
                ActivityEvent(
                    level="WARNING",
                    message="Awaiting human review before proceeding",
                    occurred_at=_iso(2),
                    elapsed_ms=1500,
                ),
            ],
            steps=[
                RunStep(
                    step_id=_new_step_id(),
                    name="Guardrail check",
                    component="Guardrail Engine",
                    status="SUCCESS",
                    start_offset_ms=0,
                    duration_ms=40,
                ),
                RunStep(
                    step_id=_new_step_id(),
                    name="Human review",
                    component="HITL Review Queue",
                    status="RUNNING",
                    start_offset_ms=40,
                    duration_ms=None,
                ),
            ],
        )

        scheduled = RunRecord(
            run_id=_new_run_id(),
            agent_id=agent_id,
            tenant_id=tenant_id,
            version=version,
            status="SUCCESS",
            trigger="SCHEDULER",
            schedule_expression="cron(0 * * * ? *)",
            started_at=_iso(3600),
            duration_ms=4200,
            cost_usd=0.02,
            is_seed_data=True,
            activity=[
                ActivityEvent(
                    level="INFO",
                    message="Triggered by schedule cron(0 * * * ? *)",
                    occurred_at=_iso(3600),
                ),
                ActivityEvent(
                    level="INFO",
                    message="LLM call completed — 890 in / 210 out",
                    occurred_at=_iso(3597),
                    elapsed_ms=3600,
                ),
                ActivityEvent(
                    level="INFO",
                    message="Response dispatched",
                    occurred_at=_iso(3596),
                    elapsed_ms=4200,
                ),
            ],
            steps=[
                RunStep(
                    step_id=_new_step_id(),
                    name="Claude 3.5 Haiku",
                    component="Amazon Bedrock",
                    status="SUCCESS",
                    start_offset_ms=0,
                    duration_ms=3600,
                    model_id="claude-3-5-haiku-20241022",
                    input_tokens=890,
                    output_tokens=210,
                    cost_usd=0.002,
                    retry_count=0,
                ),
                RunStep(
                    step_id=_new_step_id(),
                    name="Response assembly",
                    component="Agent Runtime",
                    status="SUCCESS",
                    start_offset_ms=3600,
                    duration_ms=600,
                ),
            ],
        )

        for record in (success, failed, running, scheduled):
            await self._put(record)
        return [success, failed, running, scheduled]
