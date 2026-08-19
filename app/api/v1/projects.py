"""Projects + project-scoped Agents (CLAUDE.md Section 38.2/38.6/38.7/38.11).

Project-scoped agents reuse the existing AgentRegistryStore/AgentRecord/
AgentConfiguration entirely (per the resolved design: additive project_id +
project_lifecycle_status fields, not a parallel agent system) — these
routes are thin wrappers that scope by project_id and drive the Section
38.11 draft/published/deprecated/archived lifecycle. The flat
/api/v1/agents routes (Section 5.1) and the 12-stage deployment pipeline
they trigger are completely untouched by anything in this file.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

from app.dependencies import get_project_store, get_registry_store, get_tenant_id
from app.modules.auth.dependencies import require_role
from app.modules.auth.schemas import CurrentUser
from app.modules.projects.models import ProjectRecord, ProjectStatus
from app.modules.projects.store import ProjectNotFoundError, ProjectStore
from app.modules.registry.models import (
    AgentConfiguration,
    AgentRecord,
    AgentType,
    ProjectLifecycleStatus,
)
from app.modules.registry.store import AgentRegistryStore
from app.shared.exceptions import (
    AgentNotFoundError,
    CircularDependencyError,
    InvalidRollbackError,
    VersionNotFoundError,
)
from app.shared.reference_errors import ReferencingResource, raise_if_referenced

router = APIRouter(tags=["projects"])

_READ_ROLES = ("developer", "analyst", "auditor")
_WRITE_ROLES = ("developer",)


# ── Projects ─────────────────────────────────────────────────────────────


class ProjectListResponse(BaseModel):
    items: list[ProjectRecord]


class CreateProjectRequest(BaseModel):
    name: str
    description: str
    tags: list[str] = []
    guardrail_policy_id: str | None = None


class UpdateProjectRequest(BaseModel):
    name: str | None = None
    description: str | None = None
    tags: list[str] | None = None
    guardrail_policy_id: str | None = None
    status: ProjectStatus | None = None


async def _agents_referencing_project(
    registry_store: AgentRegistryStore, tenant_id: str, project_id: str
) -> list[ReferencingResource]:
    agents = await registry_store.list_agents_by_project(tenant_id, project_id)
    return [
        ReferencingResource(
            type="agent",
            id=a.agent_id,
            name=a.name,
            project=project_id,
            status=a.project_lifecycle_status,
        )
        for a in agents
    ]


@router.get("/projects", response_model=ProjectListResponse)
async def list_projects(
    tenant_id: Annotated[str, Depends(get_tenant_id)],
    _current_user: Annotated[CurrentUser, Depends(require_role(*_READ_ROLES))],
    store: Annotated[ProjectStore, Depends(get_project_store)],
) -> ProjectListResponse:
    return ProjectListResponse(items=await store.list_projects(tenant_id))


@router.post("/projects", response_model=ProjectRecord, status_code=status.HTTP_201_CREATED)
async def create_project(
    payload: CreateProjectRequest,
    tenant_id: Annotated[str, Depends(get_tenant_id)],
    current_user: Annotated[CurrentUser, Depends(require_role(*_WRITE_ROLES))],
    store: Annotated[ProjectStore, Depends(get_project_store)],
) -> ProjectRecord:
    return await store.create(
        tenant_id=tenant_id,
        name=payload.name,
        description=payload.description,
        created_by=current_user.email,
        tags=payload.tags,
        guardrail_policy_id=payload.guardrail_policy_id,
    )


@router.get("/projects/{project_id}", response_model=ProjectRecord)
async def get_project(
    project_id: str,
    tenant_id: Annotated[str, Depends(get_tenant_id)],
    _current_user: Annotated[CurrentUser, Depends(require_role(*_READ_ROLES))],
    store: Annotated[ProjectStore, Depends(get_project_store)],
) -> ProjectRecord:
    record = await store.get(tenant_id, project_id)
    if record is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"Project {project_id!r} not found"
        )
    return record


@router.put("/projects/{project_id}", response_model=ProjectRecord)
async def update_project(
    project_id: str,
    payload: UpdateProjectRequest,
    tenant_id: Annotated[str, Depends(get_tenant_id)],
    current_user: Annotated[CurrentUser, Depends(require_role(*_WRITE_ROLES))],
    store: Annotated[ProjectStore, Depends(get_project_store)],
) -> ProjectRecord:
    try:
        return await store.update(
            tenant_id,
            project_id,
            updated_by=current_user.email,
            name=payload.name,
            description=payload.description,
            tags=payload.tags,
            guardrail_policy_id=payload.guardrail_policy_id,
            status=payload.status,
        )
    except ProjectNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc


@router.delete(
    "/projects/{project_id}", status_code=status.HTTP_204_NO_CONTENT, response_model=None
)
async def delete_project(
    project_id: str,
    tenant_id: Annotated[str, Depends(get_tenant_id)],
    _current_user: Annotated[CurrentUser, Depends(require_role(*_WRITE_ROLES))],
    store: Annotated[ProjectStore, Depends(get_project_store)],
    registry_store: Annotated[AgentRegistryStore, Depends(get_registry_store)],
) -> None:
    record = await store.get(tenant_id, project_id)
    if record is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"Project {project_id!r} not found"
        )
    raise_if_referenced(await _agents_referencing_project(registry_store, tenant_id, project_id))
    await store.delete(tenant_id, project_id)


# ── Project-scoped Agents (Section 38.6/38.7/38.11) ─────────────────────


class ProjectAgentListResponse(BaseModel):
    items: list[AgentRecord]


class CreateProjectAgentRequest(BaseModel):
    name: str
    description: str
    business_purpose: str
    agent_type: AgentType
    configuration: AgentConfiguration
    owner_email: str | None = None


class CreateProjectAgentResponse(BaseModel):
    agent_id: str
    project_id: str
    version: int
    project_lifecycle_status: ProjectLifecycleStatus | None
    created_at: str


class UpdateProjectAgentRequest(BaseModel):
    configuration: AgentConfiguration
    change_description: str


class UpdateProjectAgentResponse(BaseModel):
    agent_id: str
    version: int
    project_lifecycle_status: ProjectLifecycleStatus | None
    updated_at: str


class PublishAgentResponse(BaseModel):
    agent_id: str
    version: int
    project_lifecycle_status: ProjectLifecycleStatus | None
    updated_at: str


class ArchiveAgentResponse(BaseModel):
    agent_id: str
    project_lifecycle_status: ProjectLifecycleStatus | None
    updated_at: str


class ProjectAgentRollbackRequest(BaseModel):
    target_version: int
    reason: str


class ProjectAgentRollbackResponse(BaseModel):
    agent_id: str
    current_version: int
    project_lifecycle_status: ProjectLifecycleStatus | None
    updated_at: str


async def _require_project(
    store: ProjectStore, tenant_id: str, project_id: str
) -> ProjectRecord:
    record = await store.get(tenant_id, project_id)
    if record is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"Project {project_id!r} not found"
        )
    return record


async def _require_project_agent(
    registry_store: AgentRegistryStore, tenant_id: str, project_id: str, agent_id: str
) -> AgentRecord:
    record = await registry_store.get_agent_in_project(tenant_id, project_id, agent_id)
    if record is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Agent {agent_id!r} not found in project {project_id!r}",
        )
    return record


async def _agents_referencing_agent(
    registry_store: AgentRegistryStore, tenant_id: str, agent_id: str
) -> list[ReferencingResource]:
    """Other agents whose orchestration config still points at `agent_id`
    (as a sub-agent or a parent orchestrator) — a tenant-wide scan, same
    pattern/cost tradeoff as the other *_referencing_* helpers in this
    codebase (admin-triggered delete-check, not a hot path)."""
    referencing: list[ReferencingResource] = []
    cursor: str | None = None
    while True:
        records, cursor = await registry_store.list_agents(tenant_id, limit=100, cursor=cursor)
        for record in records:
            if record.agent_id == agent_id:
                continue
            version = await registry_store.get_version(record.agent_id, record.current_version)
            if version is None or version.configuration.orchestration is None:
                continue
            orch = version.configuration.orchestration
            sub_agent_ids = {ref.agent_id for ref in orch.sub_agents} | set(orch.sub_agent_ids)
            if agent_id in sub_agent_ids or orch.parent_orchestrator_id == agent_id:
                referencing.append(
                    ReferencingResource(
                        type="agent",
                        id=record.agent_id,
                        name=record.name,
                        project=record.project_id,
                    )
                )
        if cursor is None:
            return referencing


@router.get("/projects/{project_id}/agents", response_model=ProjectAgentListResponse)
async def list_project_agents(
    project_id: str,
    tenant_id: Annotated[str, Depends(get_tenant_id)],
    _current_user: Annotated[CurrentUser, Depends(require_role(*_READ_ROLES))],
    project_store: Annotated[ProjectStore, Depends(get_project_store)],
    registry_store: Annotated[AgentRegistryStore, Depends(get_registry_store)],
) -> ProjectAgentListResponse:
    await _require_project(project_store, tenant_id, project_id)
    agents = await registry_store.list_agents_by_project(tenant_id, project_id)
    return ProjectAgentListResponse(items=agents)


@router.post(
    "/projects/{project_id}/agents",
    response_model=CreateProjectAgentResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_project_agent(
    project_id: str,
    payload: CreateProjectAgentRequest,
    tenant_id: Annotated[str, Depends(get_tenant_id)],
    current_user: Annotated[CurrentUser, Depends(require_role(*_WRITE_ROLES))],
    project_store: Annotated[ProjectStore, Depends(get_project_store)],
    registry_store: Annotated[AgentRegistryStore, Depends(get_registry_store)],
) -> CreateProjectAgentResponse:
    await _require_project(project_store, tenant_id, project_id)
    try:
        record, _version = await registry_store.create_agent(
            tenant_id=tenant_id,
            name=payload.name,
            description=payload.description,
            business_purpose=payload.business_purpose,
            agent_type=payload.agent_type,
            configuration=payload.configuration,
            created_by=current_user.email,
            project_id=project_id,
            owner_email=payload.owner_email,
        )
    except CircularDependencyError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc

    await project_store.add_agent_id(tenant_id, project_id, record.agent_id)

    return CreateProjectAgentResponse(
        agent_id=record.agent_id,
        project_id=project_id,
        version=record.current_version,
        project_lifecycle_status=record.project_lifecycle_status,
        created_at=record.created_at,
    )


@router.get("/projects/{project_id}/agents/{agent_id}", response_model=AgentRecord)
async def get_project_agent(
    project_id: str,
    agent_id: str,
    tenant_id: Annotated[str, Depends(get_tenant_id)],
    _current_user: Annotated[CurrentUser, Depends(require_role(*_READ_ROLES))],
    registry_store: Annotated[AgentRegistryStore, Depends(get_registry_store)],
) -> AgentRecord:
    return await _require_project_agent(registry_store, tenant_id, project_id, agent_id)


@router.put(
    "/projects/{project_id}/agents/{agent_id}", response_model=UpdateProjectAgentResponse
)
async def update_project_agent(
    project_id: str,
    agent_id: str,
    payload: UpdateProjectAgentRequest,
    tenant_id: Annotated[str, Depends(get_tenant_id)],
    current_user: Annotated[CurrentUser, Depends(require_role(*_WRITE_ROLES))],
    registry_store: Annotated[AgentRegistryStore, Depends(get_registry_store)],
) -> UpdateProjectAgentResponse:
    await _require_project_agent(registry_store, tenant_id, project_id, agent_id)
    try:
        record, _version = await registry_store.update_agent(
            tenant_id=tenant_id,
            agent_id=agent_id,
            configuration=payload.configuration,
            changed_by=current_user.email,
            change_description=payload.change_description,
        )
    except AgentNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except CircularDependencyError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc

    # Section 38.11: "Edit published agent: auto-create new draft version.
    # Never mutate a published record in place." — applied to every edit,
    # not only ones starting from "published": the new version is never
    # live until it is explicitly (re)published.
    record = await registry_store.set_project_draft_after_edit(
        tenant_id, agent_id, actor=current_user.email
    )

    return UpdateProjectAgentResponse(
        agent_id=record.agent_id,
        version=record.current_version,
        project_lifecycle_status=record.project_lifecycle_status,
        updated_at=record.updated_at,
    )


@router.delete(
    "/projects/{project_id}/agents/{agent_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_model=None,
)
async def delete_project_agent(
    project_id: str,
    agent_id: str,
    tenant_id: Annotated[str, Depends(get_tenant_id)],
    _current_user: Annotated[CurrentUser, Depends(require_role(*_WRITE_ROLES))],
    registry_store: Annotated[AgentRegistryStore, Depends(get_registry_store)],
    project_store: Annotated[ProjectStore, Depends(get_project_store)],
) -> None:
    record = await _require_project_agent(registry_store, tenant_id, project_id, agent_id)

    if record.project_lifecycle_status != "archived":
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                f"Agent {agent_id!r} must be archived before it can be deleted "
                f"(current status: {record.project_lifecycle_status!r})."
            ),
        )

    raise_if_referenced(await _agents_referencing_agent(registry_store, tenant_id, agent_id))
    await registry_store.hard_delete_agent(tenant_id, agent_id)
    await project_store.remove_agent_id(tenant_id, project_id, agent_id)


@router.post(
    "/projects/{project_id}/agents/{agent_id}/publish", response_model=PublishAgentResponse
)
async def publish_project_agent(
    project_id: str,
    agent_id: str,
    tenant_id: Annotated[str, Depends(get_tenant_id)],
    current_user: Annotated[CurrentUser, Depends(require_role(*_WRITE_ROLES))],
    registry_store: Annotated[AgentRegistryStore, Depends(get_registry_store)],
) -> PublishAgentResponse:
    await _require_project_agent(registry_store, tenant_id, project_id, agent_id)
    record = await registry_store.publish_agent(tenant_id, agent_id, actor=current_user.email)
    return PublishAgentResponse(
        agent_id=record.agent_id,
        version=record.current_version,
        project_lifecycle_status=record.project_lifecycle_status,
        updated_at=record.updated_at,
    )


@router.post(
    "/projects/{project_id}/agents/{agent_id}/archive", response_model=ArchiveAgentResponse
)
async def archive_project_agent(
    project_id: str,
    agent_id: str,
    tenant_id: Annotated[str, Depends(get_tenant_id)],
    current_user: Annotated[CurrentUser, Depends(require_role(*_WRITE_ROLES))],
    registry_store: Annotated[AgentRegistryStore, Depends(get_registry_store)],
) -> ArchiveAgentResponse:
    """Not explicitly listed in the original endpoint list, but required by
    Section 38.11's own rule ("DELETE on a published agent -> 422. Must
    archive first.") — there has to be some way to reach "archived"."""
    await _require_project_agent(registry_store, tenant_id, project_id, agent_id)
    record = await registry_store.archive_agent(tenant_id, agent_id, actor=current_user.email)
    return ArchiveAgentResponse(
        agent_id=record.agent_id,
        project_lifecycle_status=record.project_lifecycle_status,
        updated_at=record.updated_at,
    )


@router.post(
    "/projects/{project_id}/agents/{agent_id}/rollback",
    response_model=ProjectAgentRollbackResponse,
)
async def rollback_project_agent(
    project_id: str,
    agent_id: str,
    payload: ProjectAgentRollbackRequest,
    tenant_id: Annotated[str, Depends(get_tenant_id)],
    current_user: Annotated[CurrentUser, Depends(require_role(*_WRITE_ROLES))],
    registry_store: Annotated[AgentRegistryStore, Depends(get_registry_store)],
) -> ProjectAgentRollbackResponse:
    await _require_project_agent(registry_store, tenant_id, project_id, agent_id)
    try:
        record = await registry_store.rollback_project_agent(
            tenant_id=tenant_id,
            agent_id=agent_id,
            target_version=payload.target_version,
            actor=current_user.email,
        )
    except VersionNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except InvalidRollbackError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    return ProjectAgentRollbackResponse(
        agent_id=record.agent_id,
        current_version=record.current_version,
        project_lifecycle_status=record.project_lifecycle_status,
        updated_at=record.updated_at,
    )
