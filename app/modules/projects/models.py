"""Project models (CLAUDE.md Section 38.2). A Project is a lightweight
grouping container for project-scoped agents (Section 38.6/38.7) — it
carries no configuration of its own, only identity/audit fields, the same
shape as the Knowledge Base / Guardrail Policy / Bedrock Credential
libraries elsewhere in this codebase.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class ProjectRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tenant_id: str
    project_id: str
    name: str
    description: str

    created_by: str
    created_at: str
    updated_by: str
    updated_at: str
    tags: dict[str, str] = Field(default_factory=dict)
