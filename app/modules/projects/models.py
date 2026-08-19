"""Project models (CLAUDE.md Section 38.2). A Project is a lightweight
grouping container for project-scoped agents (Section 38.6/38.7).

Field names/shape match the already-built UI
(panasa-agent-builder-ui/src/types/project.ts) exactly: `status` drives the
archive/restore lifecycle (Section 38.11), `agent_ids` is a maintained list
(kept in sync at the only two mutation points for project-scoped agents —
`POST .../agents` and the hard-delete path in app/api/v1/projects.py — so
there's no drift risk from an unmaintained third path), and
`guardrail_policy_id` is the optional project-level default policy applied
to agents that don't set their own (Section 38.2).
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

ProjectStatus = Literal["active", "paused", "archived"]


class ProjectRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tenant_id: str
    project_id: str
    name: str
    description: str
    owner_email: str
    status: ProjectStatus = "active"
    agent_ids: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    guardrail_policy_id: str | None = None

    created_by: str
    created_at: str
    updated_at: str
