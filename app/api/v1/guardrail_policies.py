"""Guardrail policy library (CLAUDE_Advanced_Config.md Section 4.2 / 5 / 7).

Reads open to every role; writes (create/update/delete) admin-only —
`require_role()` with no extra roles, same "admin implicitly always
allowed, empty set = admin-only" pattern as platform upgrades.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field, model_validator

from app.dependencies import get_guardrail_policy_store, get_registry_store, get_tenant_id
from app.modules.auth.dependencies import require_role
from app.modules.auth.schemas import CurrentUser
from app.modules.guardrails.models import (
    MAX_BERT_ESCALATE_THRESHOLD,
    MIN_BERT_BLOCK_THRESHOLD,
    GuardrailPolicy,
)
from app.modules.guardrails.store import GuardrailPolicyNotFoundError, GuardrailPolicyStore
from app.modules.registry.store import AgentRegistryStore

router = APIRouter(prefix="/platform/guardrail-policies", tags=["guardrail-policies"])

_READ_ROLES = ("developer", "analyst", "auditor")


class GuardrailPolicyListResponse(BaseModel):
    items: list[GuardrailPolicy]


def _validate_thresholds(bert_block_threshold: float, bert_escalate_threshold: float) -> None:
    if bert_block_threshold < MIN_BERT_BLOCK_THRESHOLD:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"bert_block_threshold cannot be set below {MIN_BERT_BLOCK_THRESHOLD}",
        )
    if bert_escalate_threshold > MAX_BERT_ESCALATE_THRESHOLD:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"bert_escalate_threshold cannot be set above {MAX_BERT_ESCALATE_THRESHOLD}",
        )


class CreateGuardrailPolicyRequest(BaseModel):
    name: str
    description: str
    input_enabled: bool = True
    bert_enabled: bool = True
    bert_model: str = "unitary/toxic-bert"
    bert_block_threshold: float = 0.85
    bert_escalate_threshold: float = 0.40
    bedrock_guardrail_id: str | None = None
    bedrock_guardrail_version: str = "DRAFT"
    output_enabled: bool = True
    output_pii_redaction: bool = True
    output_pii_entities: list[str] = Field(
        default_factory=lambda: ["NAME", "EMAIL", "PHONE", "SSN", "CREDIT_CARD", "ADDRESS"]
    )
    output_topic_blocklist: list[str] = Field(default_factory=list)
    output_profanity_filter: bool = True
    output_max_tokens: int | None = None

    @model_validator(mode="after")
    def _check_thresholds(self) -> CreateGuardrailPolicyRequest:
        _validate_thresholds(self.bert_block_threshold, self.bert_escalate_threshold)
        return self


class UpdateGuardrailPolicyRequest(BaseModel):
    """Partial update — omitted fields keep their current value."""

    name: str | None = None
    description: str | None = None
    input_enabled: bool | None = None
    bert_enabled: bool | None = None
    bert_model: str | None = None
    bert_block_threshold: float | None = None
    bert_escalate_threshold: float | None = None
    bedrock_guardrail_id: str | None = None
    bedrock_guardrail_version: str | None = None
    output_enabled: bool | None = None
    output_pii_redaction: bool | None = None
    output_pii_entities: list[str] | None = None
    output_topic_blocklist: list[str] | None = None
    output_profanity_filter: bool | None = None
    output_max_tokens: int | None = None


async def _agents_referencing_policy(
    registry_store: AgentRegistryStore, tenant_id: str, policy_id: str
) -> list[str]:
    referencing: list[str] = []
    cursor: str | None = None
    while True:
        records, cursor = await registry_store.list_agents(tenant_id, limit=100, cursor=cursor)
        for record in records:
            version = await registry_store.get_version(record.agent_id, record.current_version)
            if version is not None and version.configuration.guardrail_policy_id == policy_id:
                referencing.append(record.agent_id)
        if cursor is None:
            return referencing


@router.get("", response_model=GuardrailPolicyListResponse)
async def list_guardrail_policies(
    tenant_id: Annotated[str, Depends(get_tenant_id)],
    _current_user: Annotated[CurrentUser, Depends(require_role(*_READ_ROLES))],
    store: Annotated[GuardrailPolicyStore, Depends(get_guardrail_policy_store)],
) -> GuardrailPolicyListResponse:
    return GuardrailPolicyListResponse(items=await store.list_policies(tenant_id))


@router.post("", response_model=GuardrailPolicy, status_code=status.HTTP_201_CREATED)
async def create_guardrail_policy(
    payload: CreateGuardrailPolicyRequest,
    tenant_id: Annotated[str, Depends(get_tenant_id)],
    current_user: Annotated[CurrentUser, Depends(require_role())],
    store: Annotated[GuardrailPolicyStore, Depends(get_guardrail_policy_store)],
) -> GuardrailPolicy:
    return await store.create(
        tenant_id=tenant_id,
        name=payload.name,
        description=payload.description,
        created_by=current_user.email,
        input_enabled=payload.input_enabled,
        bert_enabled=payload.bert_enabled,
        bert_model=payload.bert_model,
        bert_block_threshold=payload.bert_block_threshold,
        bert_escalate_threshold=payload.bert_escalate_threshold,
        bedrock_guardrail_id=payload.bedrock_guardrail_id,
        bedrock_guardrail_version=payload.bedrock_guardrail_version,
        output_enabled=payload.output_enabled,
        output_pii_redaction=payload.output_pii_redaction,
        output_pii_entities=payload.output_pii_entities,
        output_topic_blocklist=payload.output_topic_blocklist,
        output_profanity_filter=payload.output_profanity_filter,
        output_max_tokens=payload.output_max_tokens,
    )


@router.get("/{policy_id}", response_model=GuardrailPolicy)
async def get_guardrail_policy(
    policy_id: str,
    tenant_id: Annotated[str, Depends(get_tenant_id)],
    _current_user: Annotated[CurrentUser, Depends(require_role(*_READ_ROLES))],
    store: Annotated[GuardrailPolicyStore, Depends(get_guardrail_policy_store)],
) -> GuardrailPolicy:
    record = await store.get(tenant_id, policy_id)
    if record is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Guardrail policy {policy_id!r} not found",
        )
    return record


@router.put("/{policy_id}", response_model=GuardrailPolicy)
async def update_guardrail_policy(
    policy_id: str,
    payload: UpdateGuardrailPolicyRequest,
    tenant_id: Annotated[str, Depends(get_tenant_id)],
    _current_user: Annotated[CurrentUser, Depends(require_role())],
    store: Annotated[GuardrailPolicyStore, Depends(get_guardrail_policy_store)],
) -> GuardrailPolicy:
    existing = await store.get(tenant_id, policy_id)
    if existing is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Guardrail policy {policy_id!r} not found",
        )
    updates = payload.model_dump(exclude_unset=True)
    _validate_thresholds(
        updates.get("bert_block_threshold", existing.bert_block_threshold),
        updates.get("bert_escalate_threshold", existing.bert_escalate_threshold),
    )
    try:
        return await store.update(tenant_id, policy_id, **updates)
    except GuardrailPolicyNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc


@router.delete("/{policy_id}", status_code=status.HTTP_204_NO_CONTENT, response_model=None)
async def delete_guardrail_policy(
    policy_id: str,
    tenant_id: Annotated[str, Depends(get_tenant_id)],
    _current_user: Annotated[CurrentUser, Depends(require_role())],
    store: Annotated[GuardrailPolicyStore, Depends(get_guardrail_policy_store)],
    registry_store: Annotated[AgentRegistryStore, Depends(get_registry_store)],
) -> None:
    record = await store.get(tenant_id, policy_id)
    if record is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Guardrail policy {policy_id!r} not found",
        )
    referencing = await _agents_referencing_policy(registry_store, tenant_id, policy_id)
    if referencing:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"Guardrail policy {policy_id!r} is still referenced by agent(s): "
                f"{', '.join(referencing)}"
            ),
        )
    await store.delete(tenant_id, policy_id)
