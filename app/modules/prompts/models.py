"""Prompt Library (Priority 2 nav addition) — saved, reusable system
prompts a developer can load into any agent's Step 2 System Prompt field
instead of retyping the same prompt across agents. Platform-wide per
tenant, same simple flat-CRUD shape as the Skills catalog, minus
versioning — a prompt is just text, there's no runtime behaviour to
snapshot.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class PromptRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    prompt_id: str
    tenant_id: str
    name: str
    content: str
    tags: list[str] = Field(default_factory=list)

    created_by: str
    created_at: str
    updated_by: str
    updated_at: str
