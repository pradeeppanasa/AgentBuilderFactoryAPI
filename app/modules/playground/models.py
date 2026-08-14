"""Playground session models (CLAUDE_Advanced_Config.md Section 5 / 8).

A playground session is a factory-internal test conversation against a
single agent's *current draft* configuration — distinct from
`app.modules.registry.transcripts` (production end-user conversations).
Kept in its own table (`panasa-playground-sessions`) so a developer
experimenting with prompt/guardrail changes never mixes into production
transcript history or counts toward production usage reports.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

PlaygroundRole = Literal["user", "assistant"]


class PlaygroundTurn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    turn_id: int
    role: PlaygroundRole
    content: str
    created_at: str


class PlaygroundSessionRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    session_id: str
    agent_id: str
    tenant_id: str
    turns: list[PlaygroundTurn] = Field(default_factory=list)
    created_at: str
    updated_at: str
