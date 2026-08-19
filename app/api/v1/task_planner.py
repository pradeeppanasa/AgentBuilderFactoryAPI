"""Task Planner (CLAUDE.md Section 38.6 Step 1 / 38.7 — A2-3).

Additive to the wizard: pre-fills steps 2-8 from a plain-English description.
Never deploys anything — returns a proposal for the user to review/edit
before the standard create-agent flow (`POST /projects/{id}/agents`) runs.
Read-only relative to persisted state, so open to every authenticated role
(same as other non-mutating analysis endpoints in this API), not gated to
developer-only.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status

from app.config import settings
from app.dependencies import (
    get_connector_catalog_store,
    get_guardrail_policy_store,
    get_knowledge_base_store,
    get_skill_store,
    get_tenant_id,
)
from app.modules.auth.dependencies import require_role
from app.modules.auth.schemas import CurrentUser
from app.modules.connectors.catalog import ConnectorCatalogStore
from app.modules.guardrails.store import GuardrailPolicyStore
from app.modules.knowledge_base.store import KnowledgeBaseStore
from app.modules.skills.store import SkillStore
from app.modules.task_planner.analyzer import analyze, analyze_architecture
from app.modules.task_planner.models import (
    TaskPlannerError,
    TaskPlannerProposal,
    TaskPlannerRequest,
    TaskPlannerResponse,
)

router = APIRouter(prefix="/platform/task-planner", tags=["task-planner"])

_ROLES = ("developer", "analyst", "auditor")


@router.post("/analyze", response_model=TaskPlannerProposal)
async def analyze_task(
    payload: TaskPlannerRequest,
    tenant_id: Annotated[str, Depends(get_tenant_id)],
    _current_user: Annotated[CurrentUser, Depends(require_role(*_ROLES))],
    connector_store: Annotated[ConnectorCatalogStore, Depends(get_connector_catalog_store)],
    kb_store: Annotated[KnowledgeBaseStore, Depends(get_knowledge_base_store)],
    guardrail_store: Annotated[GuardrailPolicyStore, Depends(get_guardrail_policy_store)],
    skill_store: Annotated[SkillStore, Depends(get_skill_store)],
) -> TaskPlannerProposal:
    try:
        return await analyze(
            description=payload.description,
            tenant_id=tenant_id,
            connector_store=connector_store,
            kb_store=kb_store,
            guardrail_store=guardrail_store,
            skill_store=skill_store,
            model_id=settings.task_planner_model_id,
            max_tokens=settings.task_planner_max_tokens,
        )
    except TaskPlannerError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)
        ) from exc


@router.post("/analyze-architecture", response_model=TaskPlannerResponse)
async def analyze_task_architecture(
    payload: TaskPlannerRequest,
    tenant_id: Annotated[str, Depends(get_tenant_id)],
    _current_user: Annotated[CurrentUser, Depends(require_role(*_ROLES))],
    connector_store: Annotated[ConnectorCatalogStore, Depends(get_connector_catalog_store)],
    kb_store: Annotated[KnowledgeBaseStore, Depends(get_knowledge_base_store)],
    guardrail_store: Annotated[GuardrailPolicyStore, Depends(get_guardrail_policy_store)],
    skill_store: Annotated[SkillStore, Depends(get_skill_store)],
) -> TaskPlannerResponse:
    """Project-level multi-agent architecture proposal (CLAUDE.md Section
    38.6 "Design corrections" — Wizard Redesign, 2026-08-18). Parallel to
    /analyze above, not a replacement for it: see the module comment in
    task_planner/models.py for why both endpoints coexist."""
    try:
        return await analyze_architecture(
            description=payload.description,
            tenant_id=tenant_id,
            connector_store=connector_store,
            kb_store=kb_store,
            guardrail_store=guardrail_store,
            skill_store=skill_store,
            model_id=settings.task_planner_model_id,
            max_tokens=settings.task_planner_max_tokens,
        )
    except TaskPlannerError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)
        ) from exc
