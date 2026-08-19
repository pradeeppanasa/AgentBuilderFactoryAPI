"""Guardrail policy library (CLAUDE.md Section 37.7/37.10/37.11 — the
2026-08-16 full nested schema expansion).

Reads open to every role; writes (create/update/delete) admin-only —
`require_role()` with no extra roles, same "admin implicitly always
allowed, empty set = admin-only" pattern as platform upgrades.

On create/update, the API auto-provisions the real Bedrock guardrail
resource behind the policy (Section 37.7: "Auto-provision Bedrock guardrail
on save") via BedrockGuardrailProvisioner, storing the returned
guardrailId/version before returning — skipped entirely when
bedrock_enabled is False, since there's nothing useful to provision.
"""

from __future__ import annotations

from typing import Annotated

from botocore.exceptions import ClientError
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field, model_validator

from app.dependencies import (
    get_bedrock_guardrail_provisioner,
    get_guardrail_policy_store,
    get_registry_store,
    get_tenant_id,
)
from app.modules.auth.dependencies import require_role
from app.modules.auth.schemas import CurrentUser
from app.modules.guardrails.models import (
    MAX_BERT_ESCALATE_THRESHOLD,
    MIN_BERT_BLOCK_THRESHOLD,
    BedrockContentFilters,
    BertConfig,
    BlockedMessages,
    ComplianceConfig,
    GuardrailPolicy,
    KeywordPolicy,
    PiiConfig,
    TopicConfig,
)
from app.modules.guardrails.provisioner import BedrockGuardrailProvisioner
from app.modules.guardrails.store import GuardrailPolicyNotFoundError, GuardrailPolicyStore
from app.modules.registry.store import AgentRegistryStore

router = APIRouter(prefix="/platform/guardrail-policies", tags=["guardrail-policies"])

_READ_ROLES = ("developer", "analyst", "auditor")


class GuardrailPolicyListResponse(BaseModel):
    items: list[GuardrailPolicy]


class ThresholdFieldError(BaseModel):
    field: str
    message: str


def _threshold_errors(
    block_threshold: float, escalate_threshold: float
) -> list[ThresholdFieldError]:
    errors: list[ThresholdFieldError] = []
    if block_threshold < MIN_BERT_BLOCK_THRESHOLD:
        errors.append(
            ThresholdFieldError(
                field="bert.block_threshold",
                message=f"cannot be set below {MIN_BERT_BLOCK_THRESHOLD}",
            )
        )
    if escalate_threshold > MAX_BERT_ESCALATE_THRESHOLD:
        errors.append(
            ThresholdFieldError(
                field="bert.escalate_threshold",
                message=f"cannot be set above {MAX_BERT_ESCALATE_THRESHOLD}",
            )
        )
    if block_threshold <= escalate_threshold:
        errors.append(
            ThresholdFieldError(
                field="bert.block_threshold",
                message="must be greater than bert.escalate_threshold",
            )
        )
    return errors


def _validate_thresholds(block_threshold: float, escalate_threshold: float) -> None:
    errors = _threshold_errors(block_threshold, escalate_threshold)
    if errors:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=[e.model_dump() for e in errors],
        )


class CreateGuardrailPolicyRequest(BaseModel):
    name: str
    description: str

    bert: BertConfig = Field(default_factory=BertConfig)

    bedrock_enabled: bool = True
    bedrock_credential_id: str | None = None
    bedrock_content_filters: BedrockContentFilters = Field(default_factory=BedrockContentFilters)

    pii: PiiConfig = Field(default_factory=PiiConfig)
    topics: TopicConfig = Field(default_factory=TopicConfig)
    keywords: KeywordPolicy = Field(default_factory=KeywordPolicy)
    compliance: ComplianceConfig = Field(default_factory=ComplianceConfig)
    blocked_messages: BlockedMessages = Field(default_factory=BlockedMessages)

    @model_validator(mode="after")
    def _check_thresholds(self) -> CreateGuardrailPolicyRequest:
        _validate_thresholds(self.bert.block_threshold, self.bert.escalate_threshold)
        return self


class UpdateGuardrailPolicyRequest(BaseModel):
    """Whole-section replace, not a deep field-by-field merge: a provided
    nested section (e.g. `bert`) replaces that entire section, matching the
    UI's single "Save" button submitting the full form state (Section
    37.14) rather than a per-field PATCH. Omitted top-level fields keep
    their current value."""

    name: str | None = None
    description: str | None = None
    bert: BertConfig | None = None
    bedrock_enabled: bool | None = None
    bedrock_credential_id: str | None = None
    bedrock_content_filters: BedrockContentFilters | None = None
    pii: PiiConfig | None = None
    topics: TopicConfig | None = None
    keywords: KeywordPolicy | None = None
    compliance: ComplianceConfig | None = None
    blocked_messages: BlockedMessages | None = None


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


# QA A-05 — botocore.exceptions.ClientError's own str() is a raw AWS SDK
# error message ("An error occurred (UnrecognizedClientException) when
# calling the CreateGuardrail operation: ...") which isn't actionable for
# an end user testing locally without real Bedrock credentials. Recognised
# credential-shaped error codes get a specific, actionable 503; anything
# else falls back to the existing generic 502.
_CREDENTIAL_ERROR_CODES = frozenset(
    {"UnrecognizedClientException", "InvalidSignatureException", "ExpiredTokenException"}
)


def _bedrock_provisioning_error(exc: Exception) -> HTTPException:
    if isinstance(exc, ClientError) and exc.response.get("Error", {}).get("Code") in (
        _CREDENTIAL_ERROR_CODES
    ):
        return HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "error": "aws_credentials_invalid",
                "message": (
                    "AWS credentials not configured. Set MOCK_BEDROCK_GUARDRAILS=true "
                    "for local testing."
                ),
            },
        )
    return HTTPException(
        status_code=status.HTTP_502_BAD_GATEWAY,
        detail=f"Bedrock guardrail provisioning failed: {exc}",
    )


async def _provision_bedrock_guardrail(
    provisioner: BedrockGuardrailProvisioner, tenant_id: str, policy: GuardrailPolicy
) -> GuardrailPolicy:
    if not policy.bedrock_enabled:
        return policy
    guardrail_id, guardrail_version = await provisioner.provision(tenant_id, policy)
    return policy.model_copy(
        update={
            "bedrock_guardrail_id": guardrail_id,
            "bedrock_guardrail_version": guardrail_version,
        }
    )


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
    provisioner: Annotated[BedrockGuardrailProvisioner, Depends(get_bedrock_guardrail_provisioner)],
) -> GuardrailPolicy:
    created = await store.create(
        tenant_id=tenant_id,
        name=payload.name,
        description=payload.description,
        created_by=current_user.email,
        bert=payload.bert,
        bedrock_enabled=payload.bedrock_enabled,
        bedrock_credential_id=payload.bedrock_credential_id,
        bedrock_content_filters=payload.bedrock_content_filters,
        pii=payload.pii,
        topics=payload.topics,
        keywords=payload.keywords,
        compliance=payload.compliance,
        blocked_messages=payload.blocked_messages,
    )
    try:
        provisioned = await _provision_bedrock_guardrail(provisioner, tenant_id, created)
    except Exception as exc:
        # A real Bedrock/AWS failure (auth, throttling, region not enabled)
        # must never leave an orphaned DynamoDB record behind that the UI
        # reported as "failed to save" — roll the just-created draft back
        # so a retry (or a corrected credential) starts clean rather than
        # accumulating half-provisioned policies.
        await store.delete(tenant_id, created.policy_id)
        raise _bedrock_provisioning_error(exc) from exc
    if provisioned is created:
        return created
    return await store.update(
        tenant_id,
        provisioned.policy_id,
        bedrock_guardrail_id=provisioned.bedrock_guardrail_id,
        bedrock_guardrail_version=provisioned.bedrock_guardrail_version,
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
    provisioner: Annotated[BedrockGuardrailProvisioner, Depends(get_bedrock_guardrail_provisioner)],
) -> GuardrailPolicy:
    existing = await store.get(tenant_id, policy_id)
    if existing is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Guardrail policy {policy_id!r} not found",
        )
    # Deliberately NOT payload.model_dump(exclude_unset=True): that dumps
    # nested sub-models (bert, pii, ...) to plain dicts, and
    # store.update()'s record.model_copy(update=...) does not re-validate
    # `update` values — the resulting GuardrailPolicy would end up with
    # e.g. `.bert` as a raw dict instead of a BertConfig instance. Using
    # the already-validated model instances directly keeps every field
    # properly typed.
    updates = {field: getattr(payload, field) for field in payload.model_fields_set}

    effective_bert = payload.bert if payload.bert is not None else existing.bert
    _validate_thresholds(effective_bert.block_threshold, effective_bert.escalate_threshold)

    try:
        updated = await store.update(tenant_id, policy_id, **updates)
    except GuardrailPolicyNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc

    try:
        provisioned = await _provision_bedrock_guardrail(provisioner, tenant_id, updated)
    except Exception as exc:
        # The field changes above are already persisted — there is no clean
        # "previous state" to roll back to (the edit itself succeeded; only
        # the Bedrock sync failed) — so this raises rather than silently
        # returning `updated` as if nothing went wrong, matching create's
        # never-swallow-a-real-AWS-failure behaviour.
        raise _bedrock_provisioning_error(exc) from exc
    if provisioned is updated:
        return updated
    return await store.update(
        tenant_id,
        policy_id,
        bedrock_guardrail_id=provisioned.bedrock_guardrail_id,
        bedrock_guardrail_version=provisioned.bedrock_guardrail_version,
    )


@router.delete("/{policy_id}", status_code=status.HTTP_204_NO_CONTENT, response_model=None)
async def delete_guardrail_policy(
    policy_id: str,
    tenant_id: Annotated[str, Depends(get_tenant_id)],
    _current_user: Annotated[CurrentUser, Depends(require_role())],
    store: Annotated[GuardrailPolicyStore, Depends(get_guardrail_policy_store)],
    registry_store: Annotated[AgentRegistryStore, Depends(get_registry_store)],
    provisioner: Annotated[
        BedrockGuardrailProvisioner, Depends(get_bedrock_guardrail_provisioner)
    ],
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
    # deprovision() is deliberately best-effort (swallows AWS-side failures,
    # see its own docstring) — no try/except needed here, unlike provision().
    await provisioner.deprovision(tenant_id, record)
    await store.delete(tenant_id, policy_id)
