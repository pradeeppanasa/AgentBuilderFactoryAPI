"""Structured 409 response shape for CLAUDE.md Section 38.11's CRUD rule:
"DELETE on any resource: check references first. If referenced -> 409 with
structured {"referenced_by": [...]} list (type, id, name, project)."

Used only by the Section 38 endpoints (Projects, project-scoped Agents,
Skills, HITL) — the pre-existing KB/GuardrailPolicy/BedrockCredential
delete-guards predate this rule and keep their own plain-string 409 detail
shape; changing those would be an unrequested behavior change to
already-tested code.
"""

from __future__ import annotations

from fastapi import HTTPException, status
from pydantic import BaseModel


class ReferencingResource(BaseModel):
    type: str
    id: str
    name: str
    project: str | None = None
    status: str | None = None


def raise_if_referenced(referencing: list[ReferencingResource]) -> None:
    if referencing:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"referenced_by": [r.model_dump() for r in referencing]},
        )
