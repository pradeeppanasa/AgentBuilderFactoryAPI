"""Tool Allowlist Registry (Sprint 3 Phase 7 — CLAUDE.md Section 62.2,
S-13a). Only a tool_id present here, with status=APPROVED, may be added
to any agent's `tools` list — enforced by
app.modules.registry.config_validator.AgentConfigValidator, checked at
create/update time (not at tool-invocation time, which is
services/agent-runtime/tool_policy.py's own separate, per-agent
allow/deny decision, Phase 4). This registry answers "does this tool
exist and has anyone reviewed it at all"; tool_policies (Phase 4)
answers "is THIS agent allowed to call it, and at what risk level".
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

RiskLevel = Literal["LOW", "MEDIUM", "HIGH", "DESTRUCTIVE"]
ToolRegistryStatus = Literal["APPROVED", "DEPRECATED", "REJECTED"]


class ToolRegistryEntry(BaseModel):
    tool_id: str
    display_name: str
    risk_level: RiskLevel
    allowed_scopes: list[str] = Field(default_factory=list)
    lambda_arn: str | None = None
    last_reviewed_at: str  # ISO 8601
    reviewed_by: str
    status: ToolRegistryStatus
