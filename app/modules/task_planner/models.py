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

from typing import Any, Literal

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


# ── Build with AI — Resource Resolution (CLAUDE.md Section 42, 2026-08-19) ──
#
# Additive, not a replacement: analyze_architecture()/TaskPlannerResponse
# above are the underlying computation ("what agents, what resources") and
# stay exactly as they are, still exercised directly by
# test_task_planner_architecture_api.py. This layer wraps that computation
# in the propose -> approve -> status flow Section 42 specifies: the
# propose step additionally classifies every resource as available/missing
# and (for missing ones) proposes a starter config; the approve step
# actually creates the approved missing resources and the proposed agents,
# server-side, from a stored session rather than trusting a client-resent
# copy of the architecture (R47/R48).

ResourceKind = Literal["tool", "knowledge_base", "guardrail_policy", "skill"]


class BuildWithAIRequest(BaseModel):
    description: str
    project_id: str | None = None


class AvailableResourceRef(BaseModel):
    """A resource the proposed architecture needs that already exists in
    the tenant's catalog — Section 42.2's "Available" rows."""

    resource_type: ResourceKind
    resource_id: str
    name: str


class MissingResourceProposal(BaseModel):
    """A resource the proposed architecture needs that does not exist yet
    — Section 42.2's "Missing" rows / Section 42.3's approval cards.
    `proposed_config` is type-specific (Section 42.5) and, per R48, never
    contains a credential value — only an `auth_type` label where relevant."""

    resource_key: str
    """Stable id for this proposal within the session (`type:name` slug) —
    what the approve request's `skip_resource_keys`/`edited_configs` refer
    to, since the resource has no real resource_id until it's created."""

    resource_type: ResourceKind
    name: str
    description: str
    proposed_config: dict[str, Any] = Field(default_factory=dict)


class ProposedAgentSpec(BaseModel):
    """One agent Task Planner recommends creating — Section 42.2's
    "Proposed" rows. `tools`/`knowledge_bases`/`skills`/`guardrail_policy`/
    `sub_agents` reference resources by name, resolved back to real/
    to-be-created ids at approve time (see executor.py)."""

    name: str
    role: Literal["standard", "orchestrator"]
    business_purpose: str
    system_prompt: str
    capability_description: str = ""
    tools: list[str] = Field(default_factory=list)
    knowledge_bases: list[str] = Field(default_factory=list)
    guardrail_policy: str | None = None
    skills: list[str] = Field(default_factory=list)
    sub_agents: list[str] = Field(default_factory=list)


class BuildWithAIProposal(BaseModel):
    session_id: str
    description: str
    available_resources: list[AvailableResourceRef] = Field(default_factory=list)
    missing_resources: list[MissingResourceProposal] = Field(default_factory=list)
    proposed_agents: list[ProposedAgentSpec] = Field(default_factory=list)
    estimated_duration_seconds: int = 15
    confidence: float = 0.5
    reasoning: str = ""


class BuildWithAIApproveRequest(BaseModel):
    session_id: str
    skip_resource_keys: list[str] = Field(default_factory=list)
    """resource_key values (from the proposal) the user chose "Skip" on —
    Section 42.3. Every other missing resource is approved as proposed."""
    edited_configs: dict[str, dict[str, Any]] = Field(default_factory=dict)
    """resource_key -> replacement proposed_config for resources the user
    chose "Edit" on — Section 42.3. Merged over (not replacing) the
    original proposed_config."""


class CreatedResourceSummary(BaseModel):
    resource_type: ResourceKind
    resource_id: str
    name: str


class CreatedAgentSummary(BaseModel):
    agent_id: str
    name: str
    role: Literal["standard", "orchestrator"]


class BuildWithAIApproveResponse(BaseModel):
    session_id: str
    created_resources: list[CreatedResourceSummary] = Field(default_factory=list)
    skipped_resource_keys: list[str] = Field(default_factory=list)
    created_agents: list[CreatedAgentSummary] = Field(default_factory=list)


BuildWithAISessionStatus = Literal["proposed", "completed", "failed"]


class BuildWithAIStatusResponse(BaseModel):
    session_id: str
    status: BuildWithAISessionStatus
    created_resources: list[CreatedResourceSummary] = Field(default_factory=list)
    created_agents: list[CreatedAgentSummary] = Field(default_factory=list)
    error: str | None = None


class BuildWithAISessionRecord(BaseModel):
    """Persisted server-side between propose and approve (DynamoDB
    `panasa-build-with-ai-sessions`, see session_store.py). Approve acts on
    this stored computation rather than trusting a client-resent copy of
    the architecture — the client only ever sends back `session_id` plus
    which resource_keys to skip/edit (R47: nothing is created without
    explicit user approval, but what gets created is server-computed)."""

    session_id: str
    tenant_id: str
    project_id: str | None = None
    description: str

    architecture: TaskPlannerResponse
    missing_resources: list[MissingResourceProposal] = Field(default_factory=list)

    status: BuildWithAISessionStatus = "proposed"
    created_resources: list[CreatedResourceSummary] = Field(default_factory=list)
    created_agents: list[CreatedAgentSummary] = Field(default_factory=list)
    error: str | None = None

    created_at: str
    updated_at: str
