"""Run records (Observability — Runs Feature, Phase 1 + Phase 2 + Phase 3).

A "Run" is one execution of the *Generated Agent Runtime* serving real
business traffic — a different thing from a Playground session (never a
production invocation, app/modules/playground/models.py) and from a
Terraform/deployment "Deployment" (app/modules/deployment/models.py).

R30/U-23: `ActivityEvent.message` and `RunStep.name`/`component` are always
pre-written, human-readable text — never raw JSON, a prompt, an LLM
response, or RAG content. There is nothing here for a UI to "parse" at
render time; sanitisation happens once, here, at write time.

Phase 2 adds `RunStep` (Section 5 — Execution Timeline/Gantt) alongside the
existing flat `activity` feed (Section 4) — same run, two views: a
chronological sentence-per-line feed, and a per-component duration
breakdown with token/cost/retry detail and (Section 6) a business-first
error format on failure.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

RunStatus = Literal["SUCCESS", "FAILED", "RUNNING", "PARTIAL"]
RunTrigger = Literal["API", "SCHEDULER", "WEBHOOK", "MANUAL", "HITL"]
ActivityLevel = Literal["INFO", "WARNING", "ERROR", "DEBUG"]
StepStatus = Literal["SUCCESS", "FAILED", "RUNNING"]


class ActivityEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    level: ActivityLevel
    message: str
    occurred_at: str
    elapsed_ms: int | None = None
    """Milliseconds since the run started — lets the UI show "(322ms)"
    next to a step without recomputing it from timestamps."""


class StepError(BaseModel):
    """Section 6 — business-first error display. `business_reason` and
    `recommended_action` are what every user sees by default;
    `raw_error_code` and the request/trace/region fields are "Technical
    details", collapsed by default (see app/modules/runs/errors.py for the
    raw-code -> (reason, action) mapping this is built from)."""

    model_config = ConfigDict(extra="forbid")

    business_reason: str
    recommended_action: str
    raw_error_code: str
    request_id: str | None = None
    trace_id: str | None = None
    region: str | None = None
    occurred_at: str


class RunStep(BaseModel):
    """One row in the Execution Timeline (Section 5). `component` is the
    system that did the work ("Amazon Bedrock", "Knowledge Base",
    "Guardrail Engine", "Tool: payroll_api") — used both as the Gantt row
    label and, on failure, as the error panel's "Component" field."""

    model_config = ConfigDict(extra="forbid")

    step_id: str
    name: str
    component: str
    status: StepStatus
    start_offset_ms: int
    """Milliseconds from run start to this step's start — what makes the
    Gantt bars positionable without re-deriving offsets from timestamps."""
    duration_ms: int | None = None
    """None while status == "RUNNING"."""

    model_id: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    cost_usd: float | None = None
    retry_count: int | None = None

    error: StepError | None = None
    """Set only when status == "FAILED"."""

    rag: RagRetrievalDetail | None = None
    """Set only on the Knowledge Base retrieval step (Phase 3, Section 9)."""


class RagDocument(BaseModel):
    """Section 9 — one retrieved chunk's relevance score. `label` is a
    document/source identifier only ("Document 1", a filename) — never the
    chunk's actual text content (R30)."""

    model_config = ConfigDict(extra="forbid")

    label: str
    relevance: float


class RagRetrievalDetail(BaseModel):
    """Section 9 — KB Retrieval panel data for a run's Knowledge Base step.
    `query` is always the literal string "[REDACTED]" (R30) — kept as a
    field, not hardcoded in the UI, so the API response shape matches the
    doc's mockup exactly without the UI inventing the redaction text."""

    model_config = ConfigDict(extra="forbid")

    query: str = "[REDACTED]"
    documents_returned: int
    relevant_count: int
    relevance_threshold: float = 0.80
    retrieval_latency_ms: int
    documents: list[RagDocument] = Field(default_factory=list)


RagasMetric = Literal[
    "faithfulness",
    "answer_relevance",
    "context_precision",
    "context_recall",
    "context_relevance",
]


class Span(BaseModel):
    """One node in the Full Trace tab's span tree (Phase 3, Section 7).
    `attributes` are scrubbed the same way as any other observability
    surface (app.modules.observability.scrubber.safe_span_attributes) —
    metadata only, never prompt/response/tool-payload content (R30/R45)."""

    model_config = ConfigDict(extra="forbid")

    span_id: str
    parent_span_id: str | None = None
    name: str
    start_offset_ms: int
    duration_ms: int | None = None
    status: StepStatus
    attributes: dict[str, str] = Field(default_factory=dict)
    tags: dict[str, str] = Field(default_factory=dict)


class RunRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_id: str
    agent_id: str
    tenant_id: str
    version: int
    status: RunStatus
    trigger: RunTrigger
    schedule_expression: str | None = None
    """Only meaningful when trigger == "SCHEDULER" (Section 11)."""

    started_at: str
    duration_ms: int | None = None
    """None while status == "RUNNING" — a run in flight has no final
    duration yet."""
    cost_usd: float | None = None

    activity: list[ActivityEvent] = Field(default_factory=list)
    steps: list[RunStep] = Field(default_factory=list)
    """Execution Timeline rows (Phase 2, Section 5) — empty for a Phase 1
    style run with only a flat activity feed."""
    spans: list[Span] = Field(default_factory=list)
    """Full Trace tab (Phase 3, Section 7) — empty unless the agent has
    tracing enabled."""
    ragas_scores: dict[RagasMetric, float] | None = None
    """RAG Evaluation panel (Phase 3, Section 9) — set only when the agent
    has a Knowledge Base AND RAGAS evaluation enabled (R13/R14)."""

    error_category: str | None = None
    """Short, stable slug derived from the failing step's raw error code
    (app.modules.runs.errors.error_category) — set only when status ==
    "FAILED". Backend-only: for filtering/grouping runs by failure type,
    never rendered as the user-facing error text (StepError.business_reason
    is what's shown; see Section 6)."""

    is_seed_data: bool = False
    """CLAUDE.md Section 40.4 — True only for synthetic records inserted by
    seed_demo_runs() (dev-only, gated behind settings.seed_runs_enabled).
    Backend-only flag for filtering/logic — never rendered as a "test data"
    badge in the UI; a seeded run must be visually indistinguishable from a
    real one, per the seeding decision this field implements."""


class RunSummary(BaseModel):
    """Agent-level analytics (Phase 3, Section 10) — Runs > All Runs header,
    aggregated over the requested window (default last 7 days)."""

    model_config = ConfigDict(extra="forbid")

    window_days: int
    total_runs: int
    success_rate: float | None = None
    """None when total_runs == 0 — no rate to report, not a fabricated 0%."""
    avg_latency_ms: float | None = None
    error_count: int
    total_tokens: int
    estimated_cost_usd: float


class LogLine(BaseModel):
    """Logs tab (Phase 3, Section 8). Sourced from the same sanitised
    ActivityEvent feed in this environment — there is no real CloudWatch
    Logs Insights proxy yet (no Generated Agent Runtime emitting real log
    groups), so this reuses already-scrubbed data rather than fabricating a
    log stream that doesn't exist."""

    model_config = ConfigDict(extra="forbid")

    timestamp: str
    level: ActivityLevel
    message: str


class LogListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    lines: list[LogLine]
