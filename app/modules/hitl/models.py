"""Human-in-the-Loop review queue (CLAUDE.md Section 38.7/38.8).

A project-scoped agent whose `AgentConfiguration.hitl.enabled` is true can
route an invocation needing a human decision into this queue instead of
answering directly. Distinct from `HumanReviewConfig` (Section 4.3/8),
which gates the *deployment pipeline* itself via Step Functions + SNS —
this queue gates individual *agent invocations* at runtime.

Note on `panasa-agents` builder-runtime's own service boundary (F8): this
Runtime is the only service in this codebase, so review creation is
reached the same way every other write in this API is — an authenticated
HTTP call. Whether that call originates from a future Generated Agent
Runtime process or is made directly during development is outside this
module's concern; it always requires a valid bearer token like every
other endpoint here.

Sprint 4 Phase 3 (S-11, R64) reuses this SAME table/record shape for a
second kind of review — a HIGH/DESTRUCTIVE-risk *tool call* awaiting
approval mid-turn — rather than building a parallel store (CLAUDE.md
Section 66.5's "reuse, don't build a second one" convention). Unlike the
`kind="pre_llm_pause"` case above, `kind="tool_approval"` reviews are
written directly to DynamoDB by `services/agent-runtime`'s own
`ToolApprovalManager` (mirroring the existing `hitl.py`/`HITLManager`
pattern), NOT through this Runtime's HTTP API — R10 forbids the Generated
Agent Runtime from depending on the Factory Runtime at all post-deploy,
so an HTTP call back here is architecturally not an option for it. The
approve/reject endpoints below are unchanged and already work for both
kinds unmodified; `tool_id`/`resume_context` exist purely so this
Runtime's own strict (`extra="forbid"`) model can read rows the other
service wrote without a validation error.
"""

from __future__ import annotations

from enum import Enum
from typing import Literal

from pydantic import BaseModel, ConfigDict


class HitlReviewStatus(str, Enum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    INFO_REQUESTED = "info_requested"


class HitlReviewRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tenant_id: str
    review_id: str
    agent_id: str
    project_id: str | None
    trigger_condition: str
    context_summary: str

    status: HitlReviewStatus
    timeout_hours: int

    requested_by: str
    requested_at: str

    reviewed_by: str | None = None
    reviewed_at: str | None = None
    decision_reason: str | None = None

    # Sprint 4 Phase 3 (S-11) — see module docstring.
    kind: Literal["pre_llm_pause", "tool_approval"] = "pre_llm_pause"
    tool_id: str | None = None
    # Opaque JSON string — owned and interpreted only by
    # services/agent-runtime's ToolApprovalManager (session_id, user_id,
    # the paused tool call, and the conversation messages needed to
    # resume the LLM round-trip once a decision is made). A string, not a
    # nested dict, so this Runtime never has to worry about DynamoDB's
    # float/Decimal handling for content it never itself constructs.
    # Never contains secrets/credentials (R61) — tool arguments and
    # message content may appear here, which is acceptable because this
    # stays inside the tenant's own DynamoDB table, never external
    # telemetry (R30).
    resume_context: str | None = None
    # Set once (conditionally, by ToolApprovalManager.try_claim_resume())
    # the moment a resume call actually acts on this review — the guard
    # against a racing/retried resume call re-invoking a DESTRUCTIVE tool.
    resumed_at: str | None = None
