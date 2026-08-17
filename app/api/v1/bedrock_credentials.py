"""Bedrock credential library (CLAUDE.md Section 37.14 — "Credential"
dropdown on the Guardrail Policy screen). Admin-only write (these are AWS
IAM role bindings, not just app config); read open to every role so the
UI dropdown can populate for non-admins viewing a policy read-only.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

from app.dependencies import (
    get_bedrock_credential_store,
    get_guardrail_policy_store,
    get_tenant_id,
)
from app.modules.auth.dependencies import require_role
from app.modules.auth.schemas import CurrentUser
from app.modules.bedrock_credentials.models import BedrockCredentialRecord
from app.modules.bedrock_credentials.store import BedrockCredentialStore
from app.modules.guardrails.store import GuardrailPolicyStore

router = APIRouter(prefix="/platform/bedrock-credentials", tags=["bedrock-credentials"])

_READ_ROLES = ("developer", "analyst", "auditor")


class BedrockCredentialListResponse(BaseModel):
    items: list[BedrockCredentialRecord]


class CreateBedrockCredentialRequest(BaseModel):
    name: str
    role_arn: str


async def _policies_referencing_credential(
    guardrail_policy_store: GuardrailPolicyStore, tenant_id: str, credential_id: str
) -> list[str]:
    policies = await guardrail_policy_store.list_policies(tenant_id)
    return [p.policy_id for p in policies if p.bedrock_credential_id == credential_id]


@router.get("", response_model=BedrockCredentialListResponse)
async def list_bedrock_credentials(
    tenant_id: Annotated[str, Depends(get_tenant_id)],
    _current_user: Annotated[CurrentUser, Depends(require_role(*_READ_ROLES))],
    store: Annotated[BedrockCredentialStore, Depends(get_bedrock_credential_store)],
) -> BedrockCredentialListResponse:
    return BedrockCredentialListResponse(items=await store.list_credentials(tenant_id))


@router.post("", response_model=BedrockCredentialRecord, status_code=status.HTTP_201_CREATED)
async def create_bedrock_credential(
    payload: CreateBedrockCredentialRequest,
    tenant_id: Annotated[str, Depends(get_tenant_id)],
    current_user: Annotated[CurrentUser, Depends(require_role())],
    store: Annotated[BedrockCredentialStore, Depends(get_bedrock_credential_store)],
) -> BedrockCredentialRecord:
    return await store.create(
        tenant_id=tenant_id,
        name=payload.name,
        role_arn=payload.role_arn,
        created_by=current_user.email,
    )


@router.get("/{credential_id}", response_model=BedrockCredentialRecord)
async def get_bedrock_credential(
    credential_id: str,
    tenant_id: Annotated[str, Depends(get_tenant_id)],
    _current_user: Annotated[CurrentUser, Depends(require_role(*_READ_ROLES))],
    store: Annotated[BedrockCredentialStore, Depends(get_bedrock_credential_store)],
) -> BedrockCredentialRecord:
    record = await store.get(tenant_id, credential_id)
    if record is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Bedrock credential {credential_id!r} not found",
        )
    return record


@router.delete("/{credential_id}", status_code=status.HTTP_204_NO_CONTENT, response_model=None)
async def delete_bedrock_credential(
    credential_id: str,
    tenant_id: Annotated[str, Depends(get_tenant_id)],
    _current_user: Annotated[CurrentUser, Depends(require_role())],
    store: Annotated[BedrockCredentialStore, Depends(get_bedrock_credential_store)],
    guardrail_policy_store: Annotated[GuardrailPolicyStore, Depends(get_guardrail_policy_store)],
) -> None:
    record = await store.get(tenant_id, credential_id)
    if record is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Bedrock credential {credential_id!r} not found",
        )
    referencing = await _policies_referencing_credential(
        guardrail_policy_store, tenant_id, credential_id
    )
    if referencing:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"Bedrock credential {credential_id!r} is still referenced by guardrail "
                f"policy(ies): {', '.join(referencing)}"
            ),
        )
    await store.delete(tenant_id, credential_id)
