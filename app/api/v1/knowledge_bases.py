"""Knowledge Base library (CLAUDE_Advanced_Config.md Section 4.1 / 5)."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

from app.dependencies import get_knowledge_base_store, get_registry_store, get_tenant_id
from app.modules.auth.dependencies import require_role
from app.modules.auth.schemas import CurrentUser
from app.modules.knowledge_base.models import EmbeddingModel, KBSourceType, KnowledgeBaseRecord
from app.modules.knowledge_base.store import KnowledgeBaseNotFoundError, KnowledgeBaseStore
from app.modules.registry.store import AgentRegistryStore

router = APIRouter(prefix="/platform/knowledge-bases", tags=["knowledge-bases"])

_READ_ROLES = ("developer", "analyst", "auditor")
_WRITE_ROLES = ("developer",)


class KnowledgeBaseListResponse(BaseModel):
    items: list[KnowledgeBaseRecord]


class CreateKnowledgeBaseRequest(BaseModel):
    name: str
    description: str
    source_type: KBSourceType
    source_config: dict[str, Any] = {}
    embedding_model: EmbeddingModel = "amazon.titan-embed-text-v2:0"
    chunk_size_tokens: int = 512
    chunk_overlap_pct: int = 10


async def _agents_referencing_kb(
    registry_store: AgentRegistryStore, tenant_id: str, kb_id: str
) -> list[str]:
    """Full-tenant scan, same pattern as lambda_handlers/validating.py's
    _build_sub_agent_graph — acceptable here for the same reason: an
    admin-triggered delete-check, not a hot path."""
    referencing: list[str] = []
    cursor: str | None = None
    while True:
        records, cursor = await registry_store.list_agents(tenant_id, limit=100, cursor=cursor)
        for record in records:
            version = await registry_store.get_version(record.agent_id, record.current_version)
            if version is not None and version.configuration.kb_id == kb_id:
                referencing.append(record.agent_id)
        if cursor is None:
            return referencing


@router.get("", response_model=KnowledgeBaseListResponse)
async def list_knowledge_bases(
    tenant_id: Annotated[str, Depends(get_tenant_id)],
    _current_user: Annotated[CurrentUser, Depends(require_role(*_READ_ROLES))],
    store: Annotated[KnowledgeBaseStore, Depends(get_knowledge_base_store)],
) -> KnowledgeBaseListResponse:
    return KnowledgeBaseListResponse(items=await store.list_knowledge_bases(tenant_id))


@router.post("", response_model=KnowledgeBaseRecord, status_code=status.HTTP_201_CREATED)
async def create_knowledge_base(
    payload: CreateKnowledgeBaseRequest,
    tenant_id: Annotated[str, Depends(get_tenant_id)],
    current_user: Annotated[CurrentUser, Depends(require_role(*_WRITE_ROLES))],
    store: Annotated[KnowledgeBaseStore, Depends(get_knowledge_base_store)],
) -> KnowledgeBaseRecord:
    return await store.create(
        tenant_id=tenant_id,
        name=payload.name,
        description=payload.description,
        source_type=payload.source_type,
        created_by=current_user.email,
        source_config=payload.source_config,
        embedding_model=payload.embedding_model,
        chunk_size_tokens=payload.chunk_size_tokens,
        chunk_overlap_pct=payload.chunk_overlap_pct,
    )


@router.get("/{kb_id}", response_model=KnowledgeBaseRecord)
async def get_knowledge_base(
    kb_id: str,
    tenant_id: Annotated[str, Depends(get_tenant_id)],
    _current_user: Annotated[CurrentUser, Depends(require_role(*_READ_ROLES))],
    store: Annotated[KnowledgeBaseStore, Depends(get_knowledge_base_store)],
) -> KnowledgeBaseRecord:
    record = await store.get(tenant_id, kb_id)
    if record is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"Knowledge base {kb_id!r} not found"
        )
    return record


@router.delete("/{kb_id}", status_code=status.HTTP_204_NO_CONTENT, response_model=None)
async def delete_knowledge_base(
    kb_id: str,
    tenant_id: Annotated[str, Depends(get_tenant_id)],
    _current_user: Annotated[CurrentUser, Depends(require_role(*_WRITE_ROLES))],
    store: Annotated[KnowledgeBaseStore, Depends(get_knowledge_base_store)],
    registry_store: Annotated[AgentRegistryStore, Depends(get_registry_store)],
) -> None:
    record = await store.get(tenant_id, kb_id)
    if record is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"Knowledge base {kb_id!r} not found"
        )
    referencing = await _agents_referencing_kb(registry_store, tenant_id, kb_id)
    if referencing:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"Knowledge base {kb_id!r} is still referenced by agent(s): "
                f"{', '.join(referencing)}"
            ),
        )
    await store.delete(tenant_id, kb_id)


@router.post("/{kb_id}/reindex", response_model=KnowledgeBaseRecord)
async def reindex_knowledge_base(
    kb_id: str,
    tenant_id: Annotated[str, Depends(get_tenant_id)],
    _current_user: Annotated[CurrentUser, Depends(require_role(*_WRITE_ROLES))],
    store: Annotated[KnowledgeBaseStore, Depends(get_knowledge_base_store)],
) -> KnowledgeBaseRecord:
    try:
        return await store.trigger_reindex(tenant_id, kb_id)
    except KnowledgeBaseNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
