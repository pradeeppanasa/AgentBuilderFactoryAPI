"""Build with AI — Resource Resolution (CLAUDE.md Section 42).

Three-step flow, per R47: nothing is created without explicit user approval.

    POST /agents/build-with-ai/propose            -> BuildWithAIProposal
    POST /agents/build-with-ai/approve             -> BuildWithAIApproveResponse
    GET  /agents/build-with-ai/{session_id}/status -> BuildWithAIStatusResponse

Mounted at the exact path Section 42.6 specifies (/agents/build-with-ai/...,
not /platform/task-planner/...) — distinct from every existing
`/agents/{agent_id}...` route's path shape, so no route ever needs to guess
whether "build-with-ai" is a literal segment or an agent_id.

`propose` computes the architecture (analyzer.analyze_architecture — unchanged,
still exercised directly by /platform/task-planner/analyze-architecture),
elaborates a starter config for whatever it found missing
(analyzer.propose_missing_resource_configs, Section 42.8 Step 2), and stores
the full computation in a session record. `approve` re-reads that stored
session rather than trusting a client-resent copy of the architecture, and
only ever acts on it once (a session already completed/failed cannot be
re-approved).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status

from app.config import settings
from app.dependencies import (
    get_build_with_ai_session_store,
    get_connector_catalog_store,
    get_guardrail_policy_store,
    get_knowledge_base_store,
    get_registry_store,
    get_skill_store,
    get_tenant_id,
)
from app.modules.auth.dependencies import require_role
from app.modules.auth.schemas import CurrentUser
from app.modules.connectors.catalog import ConnectorCatalogStore
from app.modules.guardrails.store import GuardrailPolicyStore
from app.modules.knowledge_base.store import KnowledgeBaseStore
from app.modules.registry.store import AgentRegistryStore
from app.modules.skills.store import SkillStore
from app.modules.task_planner.analyzer import analyze_architecture, propose_missing_resource_configs
from app.modules.task_planner.executor import execute_approval
from app.modules.task_planner.models import (
    AgentProposal,
    AvailableResourceRef,
    BuildWithAIApproveRequest,
    BuildWithAIApproveResponse,
    BuildWithAIProposal,
    BuildWithAIRequest,
    BuildWithAISessionRecord,
    BuildWithAIStatusResponse,
    ProposedAgentSpec,
    ResourceKind,
    TaskPlannerError,
    TaskPlannerResponse,
)
from app.modules.task_planner.session_store import (
    BuildWithAISessionNotFoundError,
    BuildWithAISessionStore,
    new_session_id,
)

router = APIRouter(prefix="/agents/build-with-ai", tags=["build-with-ai"])

# Read (propose/status) is open to the same roles as the existing Task
# Planner endpoints (nothing is created yet). Approve actually creates real
# resources/agents, so it's gated the same as POST /agents.
_READ_ROLES = ("developer", "analyst", "auditor")
_WRITE_ROLES = ("developer",)


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _available_resources(response: TaskPlannerResponse) -> list[AvailableResourceRef]:
    available: list[AvailableResourceRef] = []
    seen: set[str] = set()

    def _add(kind: ResourceKind, suggestions: list) -> None:  # type: ignore[type-arg]
        for suggestion in suggestions:
            if not suggestion.in_catalog or suggestion.resource_id is None:
                continue
            if suggestion.resource_id in seen:
                continue
            seen.add(suggestion.resource_id)
            available.append(
                AvailableResourceRef(
                    resource_type=kind, resource_id=suggestion.resource_id, name=suggestion.name
                )
            )

    _add("tool", response.resources.tools)
    _add("knowledge_base", response.resources.knowledge_bases)
    _add("guardrail_policy", response.resources.guardrail_policies)
    _add("skill", response.resources.skills)
    return available


def _proposed_agent_spec(
    proposal: AgentProposal, sub_agent_names: list[str] | None = None
) -> ProposedAgentSpec:
    return ProposedAgentSpec(
        name=proposal.name,
        role=proposal.agent_type,
        business_purpose=proposal.description,
        system_prompt=proposal.system_prompt,
        capability_description=proposal.capability_description,
        tools=[t.name for t in proposal.tools],
        knowledge_bases=[k.name for k in proposal.knowledge_bases],
        guardrail_policy=proposal.guardrail_policy.name if proposal.guardrail_policy else None,
        skills=[s.name for s in proposal.skills],
        sub_agents=sub_agent_names or [],
    )


def _proposed_agents(response: TaskPlannerResponse) -> list[ProposedAgentSpec]:
    sub_names = [sub.name for sub in response.sub_agents]
    agents = [_proposed_agent_spec(response.orchestrator, sub_names)]
    agents.extend(_proposed_agent_spec(sub) for sub in response.sub_agents)
    return agents


@router.post("/propose", response_model=BuildWithAIProposal)
async def propose_build(
    payload: BuildWithAIRequest,
    tenant_id: Annotated[str, Depends(get_tenant_id)],
    _current_user: Annotated[CurrentUser, Depends(require_role(*_READ_ROLES))],
    connector_store: Annotated[ConnectorCatalogStore, Depends(get_connector_catalog_store)],
    kb_store: Annotated[KnowledgeBaseStore, Depends(get_knowledge_base_store)],
    guardrail_store: Annotated[GuardrailPolicyStore, Depends(get_guardrail_policy_store)],
    skill_store: Annotated[SkillStore, Depends(get_skill_store)],
    session_store: Annotated[BuildWithAISessionStore, Depends(get_build_with_ai_session_store)],
) -> BuildWithAIProposal:
    try:
        architecture = await analyze_architecture(
            description=payload.description,
            tenant_id=tenant_id,
            connector_store=connector_store,
            kb_store=kb_store,
            guardrail_store=guardrail_store,
            skill_store=skill_store,
            model_id=settings.task_planner_model_id,
            max_tokens=settings.task_planner_max_tokens,
        )
        missing = await propose_missing_resource_configs(
            architecture,
            model_id=settings.task_planner_model_id,
            max_tokens=settings.task_planner_max_tokens,
        )
    except TaskPlannerError as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc

    session_id = new_session_id()
    now = _now()
    await session_store.put(
        BuildWithAISessionRecord(
            session_id=session_id,
            tenant_id=tenant_id,
            project_id=payload.project_id,
            description=payload.description,
            architecture=architecture,
            missing_resources=missing,
            status="proposed",
            created_at=now,
            updated_at=now,
        )
    )

    return BuildWithAIProposal(
        session_id=session_id,
        description=payload.description,
        available_resources=_available_resources(architecture),
        missing_resources=missing,
        proposed_agents=_proposed_agents(architecture),
        confidence=architecture.confidence,
        reasoning=architecture.reasoning,
    )


@router.post("/approve", response_model=BuildWithAIApproveResponse)
async def approve_build(
    payload: BuildWithAIApproveRequest,
    tenant_id: Annotated[str, Depends(get_tenant_id)],
    current_user: Annotated[CurrentUser, Depends(require_role(*_WRITE_ROLES))],
    connector_store: Annotated[ConnectorCatalogStore, Depends(get_connector_catalog_store)],
    kb_store: Annotated[KnowledgeBaseStore, Depends(get_knowledge_base_store)],
    guardrail_store: Annotated[GuardrailPolicyStore, Depends(get_guardrail_policy_store)],
    skill_store: Annotated[SkillStore, Depends(get_skill_store)],
    registry_store: Annotated[AgentRegistryStore, Depends(get_registry_store)],
    session_store: Annotated[BuildWithAISessionStore, Depends(get_build_with_ai_session_store)],
) -> BuildWithAIApproveResponse:
    try:
        session = await session_store.require(tenant_id, payload.session_id)
    except BuildWithAISessionNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc

    if session.status != "proposed":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"Build with AI session {payload.session_id!r} was already "
                f"{session.status} — a session can only be approved once."
            ),
        )

    try:
        created_resources, skipped, created_agents = await execute_approval(
            session=session,
            skip_resource_keys=set(payload.skip_resource_keys),
            edited_configs=payload.edited_configs,
            tenant_id=tenant_id,
            created_by=current_user.email,
            connector_store=connector_store,
            kb_store=kb_store,
            guardrail_store=guardrail_store,
            skill_store=skill_store,
            registry_store=registry_store,
        )
    except Exception as exc:
        await session_store.put(
            session.model_copy(update={"status": "failed", "error": str(exc), "updated_at": _now()})
        )
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Build with AI approval failed partway through: {exc}",
        ) from exc

    await session_store.put(
        session.model_copy(
            update={
                "status": "completed",
                "created_resources": created_resources,
                "created_agents": created_agents,
                "updated_at": _now(),
            }
        )
    )

    return BuildWithAIApproveResponse(
        session_id=payload.session_id,
        created_resources=created_resources,
        skipped_resource_keys=skipped,
        created_agents=created_agents,
    )


@router.get("/{session_id}/status", response_model=BuildWithAIStatusResponse)
async def get_build_status(
    session_id: str,
    tenant_id: Annotated[str, Depends(get_tenant_id)],
    _current_user: Annotated[CurrentUser, Depends(require_role(*_READ_ROLES))],
    session_store: Annotated[BuildWithAISessionStore, Depends(get_build_with_ai_session_store)],
) -> BuildWithAIStatusResponse:
    try:
        session = await session_store.require(tenant_id, session_id)
    except BuildWithAISessionNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc

    return BuildWithAIStatusResponse(
        session_id=session.session_id,
        status=session.status,
        created_resources=session.created_resources,
        created_agents=session.created_agents,
        error=session.error,
    )
