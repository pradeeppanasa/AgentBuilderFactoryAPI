"""Skills catalog (CLAUDE.md Section 38.3/38.11) — platform-wide, tenant-
scoped reusable prompt capabilities. Admin-only write (a shared catalog
other developers' agents depend on); read open to every role, same
pattern as the Knowledge Base / Guardrail Policy libraries.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

from app.dependencies import get_registry_store, get_skill_store, get_tenant_id
from app.modules.auth.dependencies import require_role
from app.modules.auth.schemas import CurrentUser
from app.modules.registry.store import AgentRegistryStore
from app.modules.skills.models import Skill, SkillStatus
from app.modules.skills.store import SkillNotFoundError, SkillStore
from app.shared.reference_errors import ReferencingResource, raise_if_referenced

router = APIRouter(prefix="/platform/skills", tags=["skills"])

_READ_ROLES = ("developer", "analyst", "auditor")


class SkillListResponse(BaseModel):
    items: list[Skill]


class CreateSkillRequest(BaseModel):
    name: str
    description: str
    capability: str
    prompt_fragment: str
    input_schema: dict[str, Any] = {}
    output_schema: dict[str, Any] = {}
    version: str = "1.0"


class UpdateSkillRequest(BaseModel):
    change_description: str
    name: str | None = None
    description: str | None = None
    capability: str | None = None
    prompt_fragment: str | None = None
    input_schema: dict[str, Any] | None = None
    output_schema: dict[str, Any] | None = None
    # Section 38.11's archive/restore/publish state transitions — a
    # status-only PUT (e.g. the UI's ArchivedToggle undo flow) must not
    # require change_description to describe a content edit that didn't
    # happen; SkillStore.update() skips the version bump/snapshot entirely
    # when only `status` is set.
    status: SkillStatus | None = None


async def _agents_referencing_skill(
    registry_store: AgentRegistryStore, tenant_id: str, skill_id: str
) -> list[ReferencingResource]:
    referencing: list[ReferencingResource] = []
    cursor: str | None = None
    while True:
        records, cursor = await registry_store.list_agents(tenant_id, limit=100, cursor=cursor)
        for record in records:
            version = await registry_store.get_version(record.agent_id, record.current_version)
            if version is not None and skill_id in version.configuration.skill_ids:
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


@router.get("", response_model=SkillListResponse)
async def list_skills(
    tenant_id: Annotated[str, Depends(get_tenant_id)],
    _current_user: Annotated[CurrentUser, Depends(require_role(*_READ_ROLES))],
    store: Annotated[SkillStore, Depends(get_skill_store)],
) -> SkillListResponse:
    return SkillListResponse(items=await store.list_skills(tenant_id))


@router.post("", response_model=Skill, status_code=status.HTTP_201_CREATED)
async def create_skill(
    payload: CreateSkillRequest,
    tenant_id: Annotated[str, Depends(get_tenant_id)],
    current_user: Annotated[CurrentUser, Depends(require_role())],
    store: Annotated[SkillStore, Depends(get_skill_store)],
) -> Skill:
    return await store.create(
        tenant_id=tenant_id,
        name=payload.name,
        description=payload.description,
        capability=payload.capability,
        prompt_fragment=payload.prompt_fragment,
        created_by=current_user.email,
        input_schema=payload.input_schema,
        output_schema=payload.output_schema,
        version=payload.version,
    )


@router.get("/{skill_id}", response_model=Skill)
async def get_skill(
    skill_id: str,
    tenant_id: Annotated[str, Depends(get_tenant_id)],
    _current_user: Annotated[CurrentUser, Depends(require_role(*_READ_ROLES))],
    store: Annotated[SkillStore, Depends(get_skill_store)],
) -> Skill:
    record = await store.get(tenant_id, skill_id)
    if record is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"Skill {skill_id!r} not found"
        )
    return record


@router.put("/{skill_id}", response_model=Skill)
async def update_skill(
    skill_id: str,
    payload: UpdateSkillRequest,
    tenant_id: Annotated[str, Depends(get_tenant_id)],
    current_user: Annotated[CurrentUser, Depends(require_role())],
    store: Annotated[SkillStore, Depends(get_skill_store)],
) -> Skill:
    try:
        return await store.update(
            tenant_id,
            skill_id,
            updated_by=current_user.email,
            change_description=payload.change_description,
            name=payload.name,
            description=payload.description,
            capability=payload.capability,
            prompt_fragment=payload.prompt_fragment,
            input_schema=payload.input_schema,
            output_schema=payload.output_schema,
            status=payload.status,
        )
    except SkillNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc


@router.delete("/{skill_id}", status_code=status.HTTP_204_NO_CONTENT, response_model=None)
async def delete_skill(
    skill_id: str,
    tenant_id: Annotated[str, Depends(get_tenant_id)],
    _current_user: Annotated[CurrentUser, Depends(require_role())],
    store: Annotated[SkillStore, Depends(get_skill_store)],
    registry_store: Annotated[AgentRegistryStore, Depends(get_registry_store)],
) -> None:
    record = await store.get(tenant_id, skill_id)
    if record is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"Skill {skill_id!r} not found"
        )
    raise_if_referenced(await _agents_referencing_skill(registry_store, tenant_id, skill_id))
    await store.delete(tenant_id, skill_id)
