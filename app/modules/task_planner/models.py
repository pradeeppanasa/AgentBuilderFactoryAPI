"""Task Planner (CLAUDE.md Section 38.6 Step 1 / 38.7 — A2-3).

Additive to the wizard, not a replacement: it pre-fills wizard steps 2-8
from a plain-English description. The user can skip it entirely and fill
in every step manually. It never deploys anything itself — it only
returns a proposal for the user to review/edit before the standard
create-agent flow runs.

Catalog-bound by design: the proposal must reference only resources that
already exist in the tenant's platform catalog (tools, knowledge bases,
guardrail policies, skills). Anything the LLM thinks is needed but doesn't
exist comes back with in_catalog=false and resource_id=None — never a
hallucinated id or name standing in for a real catalog entry.

Field names/shape here are dictated by the already-built UI
(panasa-agent-builder-ui/src/types/task-planner.ts, wizard Step 1
Step1Purpose.tsx) — matched exactly rather than the other way around,
since the UI was built first against CLAUDE.md Section 38.6's proposal
card layout (which shows a single suggested guardrail policy, not a list).
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from app.modules.registry.models import AgentType

# Deliberately NOT app.modules.registry.models.AgentType: that Literal has
# been progressively tightened as old agent_type values were retired
# (Section 38.6, then Section 39.1 — now just "standard"/"orchestrator").
# This endpoint's already-shipped UI contract (types/task-planner.ts,
# Step1Purpose.tsx) and its full passing test suite were built against the
# legacy vocabulary and still send/expect it — decoupling this field keeps
# that working contract intact independent of the registry's own migration.
LegacyProposalAgentType = Literal["conversational", "task", "rag", "multi-step", "orchestrator"]


class TaskPlannerRequest(BaseModel):
    description: str
    project_id: str | None = None


class CatalogSuggestion(BaseModel):
    name: str
    description: str | None = None
    in_catalog: bool
    resource_id: str | None = None  # set only when in_catalog is True


class TaskPlannerProposal(BaseModel):
    suggested_name: str
    suggested_description: str
    suggested_agent_type: LegacyProposalAgentType
    suggested_persona_name: str | None = None
    suggested_system_prompt: str

    suggested_tools: list[CatalogSuggestion] = Field(default_factory=list)
    suggested_knowledge_bases: list[CatalogSuggestion] = Field(default_factory=list)
    suggested_guardrail_policy: CatalogSuggestion | None = None
    suggested_skills: list[CatalogSuggestion] = Field(default_factory=list)
    suggested_output_format: str | None = None

    confidence: float = 0.5
    reasoning: str = ""


class TaskPlannerError(Exception):
    """Raised when the LLM fails to produce a parseable, schema-valid proposal
    after retrying once. Never silently returns a guessed/partial proposal."""


# ── Multi-agent architecture proposal (Wizard Redesign, 2026-08-18) ─────────
#
# Additive, not a replacement: TaskPlannerProposal/analyze() above stay
# exactly as they are — the already-shipped UI (Step1Purpose.tsx,
# types/task-planner.ts) and its full test suite (test_task_planner_api.py)
# depend on that single-agent shape, and CLAUDE.md Section 41 marks the
# architecture frozen with both repos already pushed. Rewriting that
# endpoint's response shape in place would silently break a working,
# tested UI flow with no corresponding UI change in this task's scope.
#
# CLAUDE.md Section 38.6's "Design corrections" describe the Task Planner
# as a project-level multi-agent architect (orchestrator + sub-agents), so
# that capability is added here as a second, parallel endpoint
# (POST /platform/task-planner/analyze-architecture) returning
# TaskPlannerResponse. The existing single-agent endpoint is unaffected;
# migrating the wizard UI onto this shape (if desired) is a separate,
# not-yet-scheduled change.


class AgentProposal(BaseModel):
    """One agent's proposed configuration within a multi-agent architecture.
    Same fields as TaskPlannerProposal's suggestion set, minus the
    top-level wrapping — an AgentProposal is used for both the orchestrator
    and every sub-agent."""

    name: str
    description: str
    agent_type: AgentType
    persona_name: str | None = None
    system_prompt: str
    # Used by an orchestrator's LLM routing_strategy to decide which
    # sub-agent to delegate to (Section 26/A1) — required for sub-agents,
    # left blank ("") for a lone standard agent with no orchestrator role.
    capability_description: str = ""

    tools: list[CatalogSuggestion] = Field(default_factory=list)
    knowledge_bases: list[CatalogSuggestion] = Field(default_factory=list)
    guardrail_policy: CatalogSuggestion | None = None
    skills: list[CatalogSuggestion] = Field(default_factory=list)


class ResourceProposal(BaseModel):
    """Deduplicated union of every resource suggestion across the
    orchestrator and all sub-agents — one place for the wizard to see
    everything the whole proposed architecture needs, independent of which
    specific agent ends up using which resource."""

    tools: list[CatalogSuggestion] = Field(default_factory=list)
    knowledge_bases: list[CatalogSuggestion] = Field(default_factory=list)
    guardrail_policies: list[CatalogSuggestion] = Field(default_factory=list)
    skills: list[CatalogSuggestion] = Field(default_factory=list)


class TaskPlannerResponse(BaseModel):
    """Multi-agent architecture proposal. For a simple, single-agent
    requirement, `orchestrator` holds the one proposed agent (typically
    agent_type="standard") and `sub_agents` is empty — there is no
    separate "single-agent" response shape; zero sub-agents is how that
    case is represented."""

    orchestrator: AgentProposal
    sub_agents: list[AgentProposal] = Field(default_factory=list)
    resources: ResourceProposal
    output_schema: str | None = None
    confidence: float = 0.5
    reasoning: str = ""
