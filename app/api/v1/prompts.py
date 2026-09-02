"""Prompt Library (Priority 2 nav addition) — saved, reusable system
prompts. Unlike Skills/KB/Guardrail Policy, a prompt is loaded by VALUE
into an agent's system_prompt field (Step 2's "Load from library"), not
attached by reference — there is nothing to guard on delete, since no
agent config ever stores a prompt_id.

Read/write both open to developer/analyst/auditor for read, developer for
write — same as the Knowledge Base library (not admin-gated like Skills,
since this is closer to personal/team scratch content than a shared
platform capability agents depend on at runtime).
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

from app.dependencies import get_prompt_store, get_tenant_id
from app.modules.auth.dependencies import require_role
from app.modules.auth.schemas import CurrentUser
from app.modules.prompts.models import PromptRecord
from app.modules.prompts.store import PromptNotFoundError, PromptStore

router = APIRouter(prefix="/platform/prompts", tags=["prompts"])

_READ_ROLES = ("developer", "analyst", "auditor")
_WRITE_ROLES = ("developer",)


class PromptListResponse(BaseModel):
    items: list[PromptRecord]


class CreatePromptRequest(BaseModel):
    name: str
    content: str
    tags: list[str] = []


class UpdatePromptRequest(BaseModel):
    name: str | None = None
    content: str | None = None
    tags: list[str] | None = None


@router.get("", response_model=PromptListResponse)
async def list_prompts(
    tenant_id: Annotated[str, Depends(get_tenant_id)],
    _current_user: Annotated[CurrentUser, Depends(require_role(*_READ_ROLES))],
    store: Annotated[PromptStore, Depends(get_prompt_store)],
) -> PromptListResponse:
    return PromptListResponse(items=await store.list_prompts(tenant_id))


@router.post("", response_model=PromptRecord, status_code=status.HTTP_201_CREATED)
async def create_prompt(
    payload: CreatePromptRequest,
    tenant_id: Annotated[str, Depends(get_tenant_id)],
    current_user: Annotated[CurrentUser, Depends(require_role(*_WRITE_ROLES))],
    store: Annotated[PromptStore, Depends(get_prompt_store)],
) -> PromptRecord:
    return await store.create(
        tenant_id=tenant_id,
        name=payload.name,
        content=payload.content,
        created_by=current_user.email,
        tags=payload.tags,
    )


@router.get("/{prompt_id}", response_model=PromptRecord)
async def get_prompt(
    prompt_id: str,
    tenant_id: Annotated[str, Depends(get_tenant_id)],
    _current_user: Annotated[CurrentUser, Depends(require_role(*_READ_ROLES))],
    store: Annotated[PromptStore, Depends(get_prompt_store)],
) -> PromptRecord:
    record = await store.get(tenant_id, prompt_id)
    if record is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"Prompt {prompt_id!r} not found"
        )
    return record


@router.put("/{prompt_id}", response_model=PromptRecord)
async def update_prompt(
    prompt_id: str,
    payload: UpdatePromptRequest,
    tenant_id: Annotated[str, Depends(get_tenant_id)],
    current_user: Annotated[CurrentUser, Depends(require_role(*_WRITE_ROLES))],
    store: Annotated[PromptStore, Depends(get_prompt_store)],
) -> PromptRecord:
    try:
        return await store.update(
            tenant_id=tenant_id,
            prompt_id=prompt_id,
            updated_by=current_user.email,
            name=payload.name,
            content=payload.content,
            tags=payload.tags,
        )
    except PromptNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc


@router.delete("/{prompt_id}", status_code=status.HTTP_204_NO_CONTENT, response_model=None)
async def delete_prompt(
    prompt_id: str,
    tenant_id: Annotated[str, Depends(get_tenant_id)],
    _current_user: Annotated[CurrentUser, Depends(require_role(*_WRITE_ROLES))],
    store: Annotated[PromptStore, Depends(get_prompt_store)],
) -> None:
    await store.delete(tenant_id, prompt_id)
