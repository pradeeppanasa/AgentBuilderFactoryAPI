"""Task Planner (CLAUDE.md Section 38.6 Step 1 / 38.7 — A2-3).

Additive to the wizard, not a replacement: it pre-fills wizard steps 2-8
from a plain-English description. The user can skip it entirely and fill
in every step manually. It never deploys anything itself — it only
returns a proposal for the user to review/edit before the standard
create-agent flow runs.

Catalog-bound by design: the proposal must reference only resources that
already exist in the tenant's platform catalog (tools, knowledge bases,
guardrail policies, skills). Anything the LLM thinks is needed but doesn't
exist comes back with catalog_status="not_found" and id=None — never a
hallucinated id or name standing in for a real catalog entry.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict

from app.modules.registry.models import AgentType

CatalogStatus = Literal["available", "not_found"]


class TaskPlannerRequest(BaseModel):
    description: str
    project_id: str | None = None


class ResourceSuggestion(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str | None = None
    name: str
    catalog_status: CatalogStatus
    reason: str


class TaskPlannerProposal(BaseModel):
    model_config = ConfigDict(extra="forbid")

    suggested_name: str
    suggested_agent_type: AgentType
    suggested_persona: str
    suggested_system_prompt: str

    tools: list[ResourceSuggestion] = []
    knowledge_bases: list[ResourceSuggestion] = []
    guardrail_policies: list[ResourceSuggestion] = []
    skills: list[ResourceSuggestion] = []

    confidence: float
    reasoning: str


class TaskPlannerError(Exception):
    """Raised when the LLM fails to produce a parseable, schema-valid proposal
    after retrying once. Never silently returns a guessed/partial proposal."""
