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
"""

from __future__ import annotations

from enum import Enum

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
