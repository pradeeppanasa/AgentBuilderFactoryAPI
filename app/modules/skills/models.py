"""Skills catalog (CLAUDE.md Section 38.3) — a reusable, versioned
prompt-capability a project-scoped agent can reference by id via
`AgentConfiguration.skill_ids` (Section 38.5).

Distinct from the older `app.modules.registry.models.SkillConfig` /
`AgentConfiguration.skills` (Section 4.9/29's built-in *platform*
capabilities — code_execution, web_search, etc., each backed by a Lambda/
IAM resource) — that concept was never implemented as its own catalog in
this codebase, so reusing the `panasa-skills` table name here for Section
38.3's different, simpler shape (a prompt fragment + I/O schema, not an
executable capability) is a name reuse, not a real collision.

"Skill edit: create new version record. Do not update agents automatically"
(Section 38.11) is implemented via `version_history` on the same item
(the user's endpoint list only calls for CRUD on /platform/skills — no
separate publish/rollback surface — so a second DynamoDB table for
version history would be unrequested scope, not a requirement)."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

# Section 38.3: "Skills are versioned independently... Upgrading to a new
# skill version is a deliberate action, not automatic" — status governs
# whether a skill is available to attach to new agents (published), still
# being authored (draft), or superseded (deprecated; existing agents
# pinned to it keep working per Section 38.11's edit table).
SkillStatus = Literal["draft", "published", "deprecated"]


class SkillVersionSnapshot(BaseModel):
    version: str
    name: str
    description: str
    capability: str
    prompt_fragment: str
    input_schema: dict[str, Any] = Field(default_factory=dict)
    output_schema: dict[str, Any] = Field(default_factory=dict)
    changed_by: str
    change_description: str
    created_at: str


class Skill(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tenant_id: str
    skill_id: str
    name: str
    description: str
    capability: str  # short capability tag, e.g. "summarization" | "classification"
    prompt_fragment: str  # the reusable prompt text/instructions

    input_schema: dict[str, Any] = Field(default_factory=dict)
    output_schema: dict[str, Any] = Field(default_factory=dict)

    version: str = "1.0"  # Section 38.3 — "1.0" style, not an integer counter
    status: SkillStatus = "draft"
    version_history: list[SkillVersionSnapshot] = Field(default_factory=list)

    created_by: str
    created_at: str
    updated_by: str
    updated_at: str
