"""Agents — CRUD + lifecycle + versioning (CLAUDE.md Section 5.1/5.2).

R01: tenant_id (via get_tenant_id) is threaded through every store call.
R08: PUT/rollback never overwrite a version — they always create a new one.
"""

from __future__ import annotations

import json
import secrets
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Annotated, Literal

import httpx
import redis.asyncio as redis
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, Field

from app.config import settings
from app.dependencies import (
    get_agent_config_validator,
    get_audit_writer,
    get_deployment_orchestrator,
    get_deployment_status_store,
    get_git_provider,
    get_iac_generator,
    get_iac_validator,
    get_metrics_emitter,
    get_pipeline_simulator,
    get_platform_settings_store,
    get_redis_client,
    get_registry_store,
    get_secrets_manager,
    get_security_audit_log_store,
    get_tenant_id,
)
from app.middleware.rate_limit import check_reveal_rate_limit
from app.modules.audit.security_log import SecurityAuditLogStore
from app.modules.audit.writer import AuditEvent, AuditWriter
from app.modules.auth.dependencies import require_role
from app.modules.auth.schemas import CurrentUser
from app.modules.change_impact.analyzer import ChangeImpactAnalyzer, ImpactAnalysis
from app.modules.deployment.models import ApprovalMode, DeploymentRecord, initial_stages
from app.modules.deployment.orchestrator import DeploymentOrchestrator
from app.modules.deployment.pipeline_simulator import DeploymentPipelineSimulator
from app.modules.deployment.status_store import DeploymentStatusStore
from app.modules.git_provider._util import agent_repo_identifier
from app.modules.git_provider.base import GitProvider
from app.modules.git_provider.github import MissingWorkflowScopeError
from app.modules.iac_generator.cicd_templates import generate_cicd_workflow
from app.modules.iac_generator.generator import IaCGenerator
from app.modules.iac_generator.policy_check_script import generate_policy_check_script
from app.modules.iac_generator.tfvars import render_terraform_tfvars
from app.modules.iac_generator.validation_models import (
    IaCValidationReport,
    TerraformValidationMode,
)
from app.modules.iac_generator.validator import IaCValidator
from app.modules.observability.metrics import MetricsEmitter
from app.modules.platform_settings.store import PlatformSettingsStore
from app.modules.registry.config_validator import AgentConfigValidator
from app.modules.registry.diff import ConfigDiff, compute_config_diff
from app.modules.registry.models import (
    AgentCapabilityContract,
    AgentConfiguration,
    AgentRecord,
    AgentStatus,
    AgentType,
    AgentVersionRecord,
    VersionStatus,
)
from app.modules.registry.store import AgentRegistryStore
from app.modules.secrets.manager import SecretNotFoundError, SecretsManager
from app.shared.exceptions import (
    AgentNotFoundError,
    CircularDependencyError,
    InvalidRollbackError,
    NoApiKeyProvisionedError,
    VersionNotFoundError,
)

router = APIRouter(prefix="/agents", tags=["agents"])
_change_impact_analyzer = ChangeImpactAnalyzer()

# Reads are open to every defined role; writes require developer (or admin — see require_role).
_READ_ROLES = ("developer", "analyst", "auditor")
_WRITE_ROLES = ("developer",)


async def _record_event(
    *,
    audit_writer: AuditWriter,
    metrics_emitter: MetricsEmitter,
    event_type: str,
    metric_name: str,
    tenant_id: str,
    agent_id: str | None,
    actor: str,
    summary: str,
    security_audit_log_store: SecurityAuditLogStore | None = None,
    security_event_type: str | None = None,
    security_action: str | None = None,
    source_ip: str = "",
) -> None:
    """Phase 14: one audit event + one CloudWatch metric per key operation
    (config_change, deploy, rollback here; block lives in
    app.modules.security.policy_enforcement — see that module for why
    guardrail_decision/tool_call have no call site in this Runtime at all).
    Both are fire-and-forget/best-effort by design (see their own modules'
    docstrings) — neither can fail the request that triggered them.

    security_audit_log_store/security_event_type (Sprint 3 Phase 2, CLAUDE.md
    Section 61) are optional and additive — a second, separate write to
    panasa-audit-log using Section 61's own (much larger) event taxonomy
    (agent.created/agent.updated/...), alongside the existing S3 AuditEvent
    write above. Only passed at call sites Phase 2 explicitly names; not
    every _record_event caller has a Section 61.2 event type yet.
    """
    await audit_writer.write(
        AuditEvent(
            event_type=event_type,
            tenant_id=tenant_id,
            agent_id=agent_id,
            actor=actor,
            summary=summary,
            occurred_at=datetime.now(UTC).isoformat(),
        )
    )
    await metrics_emitter.emit(metric_name, dimensions={"tenant_id": tenant_id})

    if security_audit_log_store is not None and security_event_type is not None:
        await security_audit_log_store.write_event(
            tenant_id=tenant_id,
            event_type=security_event_type,
            agent_id=agent_id or "",
            principal_id=actor,
            action=security_action or security_event_type,
            resource=agent_id or "",
            result="success",
            source_ip=source_ip,
        )


def _generate_api_key() -> str:
    """Sprint 3 Phase 8 (S-02). `sk-` prefix matches the UI mockup (CLAUDE.md
    Section 56.3, "API Key: sk-****...****"). 32 bytes of urlsafe randomness
    (secrets.token_urlsafe, stdlib CSPRNG) — plenty of entropy for a bearer
    credential validated via hmac.compare_digest (services/agent-runtime/
    auth.py), never guessed via brute force."""
    return f"sk-{secrets.token_urlsafe(32)}"


class CreateAgentRequest(BaseModel):
    name: str
    description: str
    business_purpose: str
    agent_type: AgentType
    configuration: AgentConfiguration
    tags: dict[str, str] = Field(default_factory=dict)
    # QA U-21: without this, create_agent() always wrote the hardcoded
    # "Initial version" as v1's change_description, discarding whatever the
    # wizard's Step 10 Changelog field actually said.
    changelog: str | None = None


class CreateAgentResponse(BaseModel):
    agent_id: str
    version: int
    status: AgentStatus
    created_at: str
    # Sprint 3 Phase 8 (S-02, R60/R61) — the raw key value, shown exactly
    # once, at the moment it's generated (never recoverable afterwards;
    # a lost key requires POST .../credentials/rotate for a fresh one).
    # None in enterprise mode — R60: Terraform's random_password inside the
    # customer VPC provisions that agent's key, never this Runtime.
    api_key: str | None = None


class RotateApiKeyResponse(BaseModel):
    agent_id: str
    api_key_secret_arn: str
    # Raw new key value, shown exactly once — same one-time-reveal
    # convention as CreateAgentResponse.api_key (R61).
    api_key: str
    previous_key_valid_until: str  # ISO 8601, for human/UI display
    rotated_at: str


class RevokeApiKeyResponse(BaseModel):
    agent_id: str
    api_key_revoked: bool
    updated_at: str


class RevealApiKeyResponse(BaseModel):
    agent_id: str
    # Raw current key value — R63: returned once per request, never cached
    # in the response object or persisted anywhere by this Runtime.
    api_key: str


class SetJwtConfigRequest(BaseModel):
    """S-12 — None for jwt_issuer/jwt_jwks_url turns JWT auth back off for
    this agent (see AgentRegistryStore.set_jwt_config's own docstring)."""

    jwt_issuer: str | None = None
    jwt_audience: str | None = None
    jwt_jwks_url: str | None = None
    jwt_tenant_claim: str = "tenant_id"


class JwtConfigResponse(BaseModel):
    agent_id: str
    jwt_issuer: str | None
    jwt_audience: str | None
    jwt_jwks_url: str | None
    jwt_tenant_claim: str
    updated_at: str


class AgentDetailResponse(BaseModel):
    agent: AgentRecord
    configuration: AgentConfiguration
    capability_contract: AgentCapabilityContract


class AgentListResponse(BaseModel):
    items: list[AgentRecord]
    next_cursor: str | None = None


class UpdateAgentRequest(BaseModel):
    configuration: AgentConfiguration
    change_description: str
    # TS01-U-06 — the Agent Wizard's Edit flow edits these top-level
    # AgentRecord fields too (Step 2), which `configuration` doesn't
    # carry. None leaves the existing value untouched, so a
    # resource-picker-only PUT (e.g. EditAgent.tsx's KB/tool pickers)
    # behaves exactly as before.
    name: str | None = None
    description: str | None = None
    business_purpose: str | None = None
    tags: dict[str, str] | None = None


class UpdateAgentResponse(BaseModel):
    agent_id: str
    version: int
    status: AgentStatus
    updated_at: str


class DeleteAgentResponse(BaseModel):
    agent_id: str
    status: AgentStatus
    updated_at: str


class AgentVersionSummary(BaseModel):
    version: int
    version_status: VersionStatus
    change_description: str
    changed_by: str
    created_at: str
    deployment_result: str | None
    rolled_back_from_version: int | None


class VersionListResponse(BaseModel):
    items: list[AgentVersionSummary]


class VersionDiffResponse(BaseModel):
    agent_id: str
    from_version: int | None
    to_version: int
    config_diff: ConfigDiff
    impact_analysis: ImpactAnalysis


class RollbackRequest(BaseModel):
    target_version: int
    reason: str


class RollbackResponse(BaseModel):
    agent_id: str
    version: int
    status: AgentStatus
    rolled_back_from_version: int
    updated_at: str
    deployment_id: str
    branch: str
    pull_request_id: str | None


class GenerateIaCResponse(BaseModel):
    agent_id: str
    version: int
    tool: str
    iac_version: str
    s3_key: str
    modules: list[str]
    validation_report: IaCValidationReport
    validation_mode: TerraformValidationMode = "local"
    environment_note: str | None = None


class IaCStageStatus(BaseModel):
    name: str
    status: Literal["completed", "pending"]


class IaCStatusResponse(BaseModel):
    """GET /agents/{agent_id}/iac/status (Wizard Redesign QA A-04/U-08).

    generate-iac renders + validates synchronously in a single request (pure
    Jinja2 templating plus a local `terraform fmt`/`validate` — no network
    calls, no long-running job), so there is no in-progress state to observe
    between polls: this endpoint reports the outcome of the most recent
    completed generate-iac call, not a live-updating background job. A
    caller that polls immediately after triggering generate-iac will see
    "completed"/"failed" on its very first poll."""

    agent_id: str
    version: int
    status: Literal["not_started", "completed", "failed"]
    stages: list[IaCStageStatus]
    validation: IaCValidationReport | None = None


class DeployResponse(BaseModel):
    agent_id: str
    version: int
    deployment_id: str
    status: AgentStatus
    branch: str
    pull_request_id: str | None


class DeploymentListResponse(BaseModel):
    items: list[DeploymentRecord]


class CloneAgentRequest(BaseModel):
    name: str


@router.post(
    "/{agent_id}/clone", response_model=AgentDetailResponse, status_code=status.HTTP_201_CREATED
)
async def clone_agent(
    agent_id: str,
    payload: CloneAgentRequest,
    tenant_id: Annotated[str, Depends(get_tenant_id)],
    current_user: Annotated[CurrentUser, Depends(require_role(*_WRITE_ROLES))],
    store: Annotated[AgentRegistryStore, Depends(get_registry_store)],
    audit_writer: Annotated[AuditWriter, Depends(get_audit_writer)],
    metrics_emitter: Annotated[MetricsEmitter, Depends(get_metrics_emitter)],
) -> AgentDetailResponse:
    """Clone / Fork Agent — copies the source's current configuration
    (system prompt, tools, KB, model, HITL, memory, guardrail, everything)
    into a brand new agent, owned by the caller's own tenant, at DRAFT v1.
    Never copies deployment/run/version history — those stay under the
    source agent_id. The source is only ever read here."""
    try:
        record, version = await store.clone_agent(
            tenant_id=tenant_id,
            source_agent_id=agent_id,
            new_name=payload.name,
            created_by=current_user.email,
        )
    except AgentNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except CircularDependencyError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc

    await _record_event(
        audit_writer=audit_writer,
        metrics_emitter=metrics_emitter,
        event_type="config_change",
        metric_name="AgentCloned",
        tenant_id=tenant_id,
        agent_id=record.agent_id,
        actor=current_user.email,
        summary=f"Agent {record.agent_id!r} cloned from {agent_id!r}",
    )

    return AgentDetailResponse(
        agent=record,
        configuration=version.configuration,
        capability_contract=version.capability_contract,
    )


@router.post("", response_model=CreateAgentResponse, status_code=status.HTTP_201_CREATED)
async def create_agent(
    payload: CreateAgentRequest,
    tenant_id: Annotated[str, Depends(get_tenant_id)],
    current_user: Annotated[CurrentUser, Depends(require_role(*_WRITE_ROLES))],
    store: Annotated[AgentRegistryStore, Depends(get_registry_store)],
    audit_writer: Annotated[AuditWriter, Depends(get_audit_writer)],
    metrics_emitter: Annotated[MetricsEmitter, Depends(get_metrics_emitter)],
    security_audit_log_store: Annotated[
        SecurityAuditLogStore, Depends(get_security_audit_log_store)
    ],
    agent_config_validator: Annotated[AgentConfigValidator, Depends(get_agent_config_validator)],
    secrets_manager: Annotated[SecretsManager, Depends(get_secrets_manager)],
) -> CreateAgentResponse:
    validation_errors = await agent_config_validator.validate(payload.configuration, tenant_id)
    if validation_errors:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"errors": validation_errors},
        )

    try:
        record, _version = await store.create_agent(
            tenant_id=tenant_id,
            name=payload.name,
            description=payload.description,
            business_purpose=payload.business_purpose,
            agent_type=payload.agent_type,
            configuration=payload.configuration,
            created_by=current_user.email,
            tags=payload.tags,
            changelog=payload.changelog,
        )
    except CircularDependencyError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc

    await _record_event(
        audit_writer=audit_writer,
        metrics_emitter=metrics_emitter,
        event_type="config_change",
        metric_name="AgentCreated",
        tenant_id=tenant_id,
        agent_id=record.agent_id,
        actor=current_user.email,
        summary=f"Agent {record.agent_id!r} created (v{record.current_version})",
        security_audit_log_store=security_audit_log_store,
        security_event_type="agent.created",
        security_action="create",
    )

    # Sprint 3 Phase 8 (S-02, R60) — prototype mode only. Enterprise mode
    # leaves api_key_secret_arn unset here; it's populated later from
    # Terraform's own random_password output (Section 56.5/56.6, DEP-INF-
    # 01/02/03 — a separate, not-yet-built deployment-pipeline gap tracked
    # in CLAUDE.md Section 59/60, out of this Sprint 3 security phase).
    raw_api_key: str | None = None
    if settings.deployment_mode == "prototype":
        raw_api_key = _generate_api_key()
        api_key_secret_arn = await secrets_manager.create_secret(
            f"panasa-{record.agent_id}-api-key", raw_api_key
        )
        record = await store.provision_api_key(
            tenant_id=tenant_id,
            agent_id=record.agent_id,
            api_key_secret_arn=api_key_secret_arn,
            updated_by=current_user.email,
        )
        await _record_event(
            audit_writer=audit_writer,
            metrics_emitter=metrics_emitter,
            event_type="config_change",
            metric_name="AgentCredentialCreated",
            tenant_id=tenant_id,
            agent_id=record.agent_id,
            actor=current_user.email,
            summary=f"API key provisioned for agent {record.agent_id!r}",
            security_audit_log_store=security_audit_log_store,
            security_event_type="credential.created",
            security_action="create",
        )

    return CreateAgentResponse(
        agent_id=record.agent_id,
        version=record.current_version,
        status=record.status,
        created_at=record.created_at,
        api_key=raw_api_key,
    )


@router.get("", response_model=AgentListResponse)
async def list_agents(
    tenant_id: Annotated[str, Depends(get_tenant_id)],
    _current_user: Annotated[CurrentUser, Depends(require_role(*_READ_ROLES))],
    store: Annotated[AgentRegistryStore, Depends(get_registry_store)],
    status_filter: Annotated[AgentStatus | None, Query(alias="status")] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    cursor: Annotated[str | None, Query()] = None,
) -> AgentListResponse:
    records, next_cursor = await store.list_agents(
        tenant_id=tenant_id, status=status_filter, limit=limit, cursor=cursor
    )
    return AgentListResponse(items=records, next_cursor=next_cursor)


@router.get("/{agent_id}", response_model=AgentDetailResponse)
async def get_agent(
    agent_id: str,
    tenant_id: Annotated[str, Depends(get_tenant_id)],
    _current_user: Annotated[CurrentUser, Depends(require_role(*_READ_ROLES))],
    store: Annotated[AgentRegistryStore, Depends(get_registry_store)],
) -> AgentDetailResponse:
    record = await store.get_agent(tenant_id, agent_id)
    if record is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"Agent {agent_id!r} not found"
        )

    version = await store.get_version(agent_id, record.current_version)
    if version is None:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Current version {record.current_version} missing for agent {agent_id!r}",
        )

    return AgentDetailResponse(
        agent=record,
        configuration=version.configuration,
        capability_contract=version.capability_contract,
    )


@router.put("/{agent_id}", response_model=UpdateAgentResponse)
async def update_agent(
    agent_id: str,
    payload: UpdateAgentRequest,
    tenant_id: Annotated[str, Depends(get_tenant_id)],
    current_user: Annotated[CurrentUser, Depends(require_role(*_WRITE_ROLES))],
    store: Annotated[AgentRegistryStore, Depends(get_registry_store)],
    audit_writer: Annotated[AuditWriter, Depends(get_audit_writer)],
    metrics_emitter: Annotated[MetricsEmitter, Depends(get_metrics_emitter)],
    security_audit_log_store: Annotated[
        SecurityAuditLogStore, Depends(get_security_audit_log_store)
    ],
    agent_config_validator: Annotated[AgentConfigValidator, Depends(get_agent_config_validator)],
) -> UpdateAgentResponse:
    validation_errors = await agent_config_validator.validate(payload.configuration, tenant_id)
    if validation_errors:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"errors": validation_errors},
        )

    try:
        record, _version = await store.update_agent(
            tenant_id=tenant_id,
            agent_id=agent_id,
            configuration=payload.configuration,
            changed_by=current_user.email,
            change_description=payload.change_description,
            name=payload.name,
            description=payload.description,
            business_purpose=payload.business_purpose,
            tags=payload.tags,
        )
    except AgentNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except CircularDependencyError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc

    await _record_event(
        audit_writer=audit_writer,
        metrics_emitter=metrics_emitter,
        event_type="config_change",
        metric_name="AgentUpdated",
        tenant_id=tenant_id,
        agent_id=agent_id,
        actor=current_user.email,
        summary=(
            f"Agent {agent_id!r} updated to v{record.current_version}: "
            f"{payload.change_description}"
        ),
        security_audit_log_store=security_audit_log_store,
        security_event_type="agent.updated",
        security_action="update",
    )

    return UpdateAgentResponse(
        agent_id=record.agent_id,
        version=record.current_version,
        status=record.status,
        updated_at=record.updated_at,
    )


@router.delete("/{agent_id}", response_model=DeleteAgentResponse)
async def delete_agent(
    agent_id: str,
    tenant_id: Annotated[str, Depends(get_tenant_id)],
    current_user: Annotated[CurrentUser, Depends(require_role(*_WRITE_ROLES))],
    store: Annotated[AgentRegistryStore, Depends(get_registry_store)],
    audit_writer: Annotated[AuditWriter, Depends(get_audit_writer)],
    metrics_emitter: Annotated[MetricsEmitter, Depends(get_metrics_emitter)],
    security_audit_log_store: Annotated[
        SecurityAuditLogStore, Depends(get_security_audit_log_store)
    ],
) -> DeleteAgentResponse:
    try:
        record = await store.soft_delete_agent(
            tenant_id=tenant_id, agent_id=agent_id, updated_by=current_user.email
        )
    except AgentNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc

    await _record_event(
        audit_writer=audit_writer,
        metrics_emitter=metrics_emitter,
        event_type="config_change",
        metric_name="AgentDeprecated",
        tenant_id=tenant_id,
        agent_id=agent_id,
        actor=current_user.email,
        summary=f"Agent {agent_id!r} soft-deleted (status={record.status})",
        # Sprint 4 Phase 2 (S-05) — Section 61.2's own taxonomy name, added
        # alongside the existing "config_change" old-taxonomy write (Section
        # 14) rather than replacing it — see _record_event's own docstring
        # for why both systems coexist.
        security_audit_log_store=security_audit_log_store,
        security_event_type="agent.deleted",
        security_action="delete",
    )

    return DeleteAgentResponse(
        agent_id=record.agent_id, status=record.status, updated_at=record.updated_at
    )


def _to_summary(version: AgentVersionRecord) -> AgentVersionSummary:
    return AgentVersionSummary(
        version=version.version,
        version_status=version.version_status,
        change_description=version.change_description,
        changed_by=version.changed_by,
        created_at=version.created_at,
        deployment_result=version.deployment_result,
        rolled_back_from_version=version.rolled_back_from_version,
    )


@router.get("/{agent_id}/versions", response_model=VersionListResponse)
async def list_versions(
    agent_id: str,
    tenant_id: Annotated[str, Depends(get_tenant_id)],
    _current_user: Annotated[CurrentUser, Depends(require_role(*_READ_ROLES))],
    store: Annotated[AgentRegistryStore, Depends(get_registry_store)],
) -> VersionListResponse:
    try:
        versions = await store.list_versions(tenant_id, agent_id)
    except AgentNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc

    return VersionListResponse(items=[_to_summary(v) for v in versions])


@router.get("/{agent_id}/versions/{version}", response_model=AgentVersionRecord)
async def get_version(
    agent_id: str,
    version: int,
    tenant_id: Annotated[str, Depends(get_tenant_id)],
    _current_user: Annotated[CurrentUser, Depends(require_role(*_READ_ROLES))],
    store: Annotated[AgentRegistryStore, Depends(get_registry_store)],
) -> AgentVersionRecord:
    try:
        version_record = await store.get_version_detail(tenant_id, agent_id, version)
    except AgentNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc

    if version_record is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Version {version} of agent {agent_id!r} not found",
        )
    return version_record


@router.get("/{agent_id}/versions/{version}/diff", response_model=VersionDiffResponse)
async def get_version_diff(
    agent_id: str,
    version: int,
    tenant_id: Annotated[str, Depends(get_tenant_id)],
    _current_user: Annotated[CurrentUser, Depends(require_role(*_READ_ROLES))],
    store: Annotated[AgentRegistryStore, Depends(get_registry_store)],
) -> VersionDiffResponse:
    try:
        to_version = await store.get_version_detail(tenant_id, agent_id, version)
    except AgentNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc

    if to_version is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Version {version} of agent {agent_id!r} not found",
        )

    from_version_number = version - 1 if version > 1 else None
    from_version = (
        await store.get_version_detail(tenant_id, agent_id, from_version_number)
        if from_version_number is not None
        else None
    )

    diff = compute_config_diff(
        from_version.configuration if from_version else None, to_version.configuration
    )
    impact_analysis = _change_impact_analyzer.analyze_diff(diff)
    return VersionDiffResponse(
        agent_id=agent_id,
        from_version=from_version_number,
        to_version=version,
        config_diff=diff,
        impact_analysis=impact_analysis,
    )


@dataclass
class _TriggeredDeployment:
    deployment_id: str
    branch: str
    pull_request_id: str | None
    updated_record: AgentRecord


def _generate_agent_repo_readme(*, agent_id: str, version: int, deployment_id: str) -> str:
    """Section 45.2 — "auto-generated, describes the agent + version"."""
    return (
        f"# {agent_id}\n\n"
        f"Generated Terraform for Panasa agent `{agent_id}`.\n\n"
        f"- Current version: {version}\n"
        f"- Last deployment: {deployment_id}\n\n"
        "This repository is managed entirely by the Panasa Agent Builder "
        "Runtime (CLAUDE.md Section 45.2). Its Terraform is always "
        "generated from the agent's configuration and pushed here "
        "automatically on every deploy — do not edit it by hand.\n"
    )


async def _trigger_deployment(
    *,
    tenant_id: str,
    agent_id: str,
    version: int,
    configuration: AgentConfiguration,
    triggered_by: str,
    store: AgentRegistryStore,
    iac_generator: IaCGenerator,
    git_provider: GitProvider,
    deployment_orchestrator: DeploymentOrchestrator,
    deployment_status_store: DeploymentStatusStore,
    platform_settings_store: PlatformSettingsStore,
    background_tasks: BackgroundTasks,
    pipeline_simulator: DeploymentPipelineSimulator,
) -> _TriggeredDeployment:
    """Shared by deploy_agent and rollback_agent (Phase 13: "Rollback
    endpoint creates new version from old config, triggers deployment") —
    generate IaC, open the F5 PR, publish the EventBridge event, and record
    the trigger. R22: the version being replaced stays LIVE throughout;
    nothing here touches live_version — only MarkActive (Phase 11) does,
    once HEALTH_CHECK passes.
    """
    # Section 45.2 — one private repo per agent (panasa-iac-{agent_id}),
    # not the single shared GIT_REPO_URL.
    repo = agent_repo_identifier(settings.git_provider, settings.git_org, agent_id)
    if repo is None:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="GIT_ORG is not configured",
        )

    deployment_id = f"DEP-{uuid.uuid4().hex[:8].upper()}"

    # Section 45.3/45.13 (R50, resolved as configurable — see
    # deployment/models.py's module docstring): read once, at trigger time,
    # so a later change to the tenant's default never affects a deployment
    # already in flight.
    tenant_settings = await platform_settings_store.get_or_create(tenant_id, triggered_by)
    approval_mode: ApprovalMode = tenant_settings.default_approval_mode

    # TS02-A-03: every external call below (S3/IaC generation, the git
    # provider's real network calls) can fail for reasons entirely outside
    # this request — a git token that's invalid/expired/unconfigured, the
    # IaC bucket unreachable, etc. Before this fix, any of those surfaced
    # as a bare, bodyless 500 (an unhandled httpx.HTTPStatusError or
    # botocore ClientError propagating straight out of the route). The UI
    # must always get a structured, actionable error instead (Fix 3 in the
    # TS02 bug report) — matching the same convention already used for
    # LLM/guardrail/KB provisioning failures elsewhere in this API.
    try:
        iac_result = await iac_generator.generate(
            agent_id=agent_id, tenant_id=tenant_id, version=version, config=configuration
        )
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail={
                "error": "iac_generation_failed",
                "message": f"Could not generate infrastructure for deployment: {exc}",
            },
        ) from exc

    await store.record_iac_artifact(
        tenant_id=tenant_id,
        agent_id=agent_id,
        version=version,
        iac_version=iac_result.iac_version,
        iac_s3_key=iac_result.s3_key,
    )

    now = datetime.now(UTC).isoformat()
    await deployment_status_store.create_deployment(
        DeploymentRecord(
            agent_id=agent_id,
            deployment_id=deployment_id,
            version=version,
            triggered_by=triggered_by,
            triggered_at=now,
            approval_mode=approval_mode,
            stages=initial_stages(),
            iac_s3_key=iac_result.s3_key,
            updated_at=now,
        )
    )

    try:
        # Section 45.3: v1 (repo doesn't exist yet) pushes straight to the
        # default branch, no PR. v2+ (repo already exists) always goes
        # through a branch + PR, even if this happens to be a re-deploy of
        # version 1 against an already-created repo — repo existence, not
        # the raw version number, is what the spec keys this on.
        repo_already_existed = await git_provider.repository_exists(repo)
        if not repo_already_existed:
            await git_provider.create_repository(repo)

        files = {
            **iac_result.files,
            "README.md": _generate_agent_repo_readme(
                agent_id=agent_id, version=version, deployment_id=deployment_id
            ),
        }
        # Section 45.6/R58 — the workflow file is a per-repo artifact,
        # committed once alongside the repo's first real Terraform content.
        # Changing the tenant's cicd_provider/approval_mode later never
        # rewrites an already-committed workflow file (same "read once, at
        # creation" rule as approval_mode itself — see
        # PlatformSettingsRecord.cicd_provider's docstring).
        #
        # `repo_already_existed` alone isn't a reliable "already committed"
        # signal: create_repository() can succeed and then this same
        # attempt's commit_files() call can still fail (expired token,
        # transient network error) before ever writing the workflow file —
        # a retried deploy then sees an existing repo that never actually
        # got it. Checking the file's real presence on the default branch
        # covers both the normal v2+ case (already there, skip) and that
        # failed-first-attempt case (repo exists, file doesn't).
        workflow_path, workflow_content = generate_cicd_workflow(
            tenant_settings.cicd_provider, approval_mode, agent_id
        )
        workflow_already_committed = repo_already_existed and await git_provider.file_exists(
            repo, workflow_path, branch=settings.git_default_branch
        )
        if not workflow_already_committed:
            files[workflow_path] = workflow_content

        # Same signal, a second use: whether this repo has ever received a
        # real deploy's content before — true v1 (repo didn't exist) is one
        # case, but a repo that "exists" only because an earlier attempt's
        # create_repository() succeeded and then that same attempt's own
        # commit_files() failed (expired token, GitHub's Git Data API
        # eventual consistency — see GitProvider.commit_files's docstring)
        # is functionally identical: nothing but the auto-init commit is
        # really there. Both get the same omit_base_tree=True treatment
        # below, regardless of which of the two branches immediately after
        # this actually runs (push straight to main, or branch + PR).
        repo_has_no_real_content_yet = not workflow_already_committed

        # Generic Agent Runtime instruction (2026-09-03) — unlike the
        # workflow file above, tfvars values are as config-driven as the
        # Terraform itself: regenerated on every deploy so a tenant fixing
        # a wrong VPC ID/subnet takes effect on the very next deploy rather
        # than being stuck like the workflow file deliberately is.
        files[f"terraform/agents/{agent_id}/terraform.auto.tfvars.json"] = render_terraform_tfvars(
            tenant_settings, settings.aws_region, configuration, iac_result.modules
        )

        # Part 6 — the generated workflow's final step POSTs this file's
        # content straight to POST /internal/deployment-complete after a
        # real terraform apply succeeds. Regenerated every deploy (this
        # exact deployment_id/version pair only exists for this one run).
        files[f"terraform/agents/{agent_id}/deployment-metadata.json"] = json.dumps(
            {
                "agent_id": agent_id,
                "tenant_id": tenant_id,
                "deployment_id": deployment_id,
                "version": version,
                "status": "ACTIVE",
            }
        )

        # CLAUDE.md instruction (2026-09-07, item 5) — the generated GitHub
        # Actions workflow's POLICY_CHECK stage runs this directly (`python
        # {path} {tf_dir}`); regenerated every deploy, same reasoning as
        # tfvars.json above (pure, agent-independent code — nothing to
        # preserve between deploys, always current with this Runtime's own
        # checks).
        policy_check_script_path, policy_check_script_content = generate_policy_check_script()
        files[policy_check_script_path] = policy_check_script_content

        pull_request_id: str | None
        if repo_already_existed:
            branch = f"deploy/v{version}-{deployment_id}"
            await git_provider.create_branch(repo, branch, from_branch=settings.git_default_branch)
            await git_provider.commit_files(
                repo,
                branch,
                files,
                message=f"Agent {agent_id} v{version} — generated {iac_result.tool} IaC",
                # See repo_has_no_real_content_yet's definition above — a
                # repo that "exists" only via a prior attempt's
                # create_repository() call, never a real commit, needs the
                # exact same base_tree-skipping treatment as true v1 below.
                # `branch` was just cut from the default branch, which is
                # itself still just the auto-init commit in that case, so
                # `files` (complete/exhaustive, same as the v1 case) is
                # equally safe to commit without a base_tree reference.
                omit_base_tree=repo_has_no_real_content_yet,
            )
            pull_request_id = await git_provider.create_pull_request(
                repo,
                branch,
                title=f"[Panasa Auto] {agent_id} v{version} — Deploy",
                description=(
                    f"Agent: {agent_id} | Version: {version} | Impact: PENDING\n"
                    "Security: pending | RAGAS: pending"
                ),
            )
        else:
            branch = settings.git_default_branch
            await git_provider.commit_files(
                repo,
                branch,
                files,
                message=f"Agent {agent_id} v{version} — generated {iac_result.tool} IaC",
                # `files` is complete and exhaustive here — the repo has
                # nothing on it yet but create_repository()'s own auto-init
                # commit, which `files` already supersedes (its own
                # README.md). Safe to skip base_tree entirely (GitHub only
                # — see GitProvider.commit_files's docstring) rather than
                # depend on that just-created commit's tree being
                # queryable yet.
                omit_base_tree=True,
            )
            pull_request_id = None
    except MissingWorkflowScopeError as exc:
        message = str(exc)
        await deployment_status_store.update_stage(
            agent_id,
            deployment_id,
            stage="GENERATING_IAC",
            stage_status="FAILED",
            output_summary=message,
            overall_status="FAILED",
            failure_reason=message,
            failed_stage="GENERATING_IAC",
        )
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail={"error": "git_provider_missing_workflow_scope", "message": message},
        ) from exc
    except httpx.HTTPStatusError as exc:
        message = (
            f"Git provider rejected the request ({exc.response.status_code}). "
            "Check that GIT_CREDENTIALS_SECRET holds a valid, unexpired token with "
            "write access to the configured repository."
            if exc.response.status_code in (401, 403)
            else f"Git provider request failed: {exc}"
        )
        await deployment_status_store.update_stage(
            agent_id,
            deployment_id,
            stage="GENERATING_IAC",
            stage_status="FAILED",
            output_summary=message,
            overall_status="FAILED",
            failure_reason=message,
            failed_stage="GENERATING_IAC",
        )
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail={"error": "git_provider_failed", "message": message},
        ) from exc
    except httpx.HTTPError as exc:
        message = f"Could not reach the git provider: {exc}"
        await deployment_status_store.update_stage(
            agent_id,
            deployment_id,
            stage="GENERATING_IAC",
            stage_status="FAILED",
            output_summary=message,
            overall_status="FAILED",
            failure_reason=message,
            failed_stage="GENERATING_IAC",
        )
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail={"error": "git_provider_unreachable", "message": message},
        ) from exc

    await deployment_status_store.record_git_reference(
        agent_id, deployment_id, branch=branch, pull_request_id=pull_request_id
    )

    try:
        await deployment_orchestrator.trigger_deployment(
            agent_id=agent_id, version=version, deployment_id=deployment_id, tenant_id=tenant_id
        )
    except Exception as exc:
        message = f"Could not start the deployment pipeline: {exc}"
        await deployment_status_store.update_stage(
            agent_id,
            deployment_id,
            stage="GENERATING_IAC",
            stage_status="FAILED",
            output_summary=message,
            overall_status="FAILED",
            failure_reason=message,
            failed_stage="GENERATING_IAC",
        )
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail={"error": "deployment_trigger_failed", "message": message},
        ) from exc

    updated_record = await store.record_deployment_trigger(
        tenant_id=tenant_id,
        agent_id=agent_id,
        version=version,
        deployment_id=deployment_id,
        updated_by=triggered_by,
    )

    if settings.simulate_deployment_pipeline:
        background_tasks.add_task(
            pipeline_simulator.run,
            tenant_id,
            agent_id,
            deployment_id,
            config=configuration,
            iac_files=iac_result.files,
        )

    return _TriggeredDeployment(
        deployment_id=deployment_id,
        branch=branch,
        pull_request_id=pull_request_id,
        updated_record=updated_record,
    )


@router.post("/{agent_id}/rollback", response_model=RollbackResponse)
async def rollback_agent(
    agent_id: str,
    payload: RollbackRequest,
    tenant_id: Annotated[str, Depends(get_tenant_id)],
    current_user: Annotated[CurrentUser, Depends(require_role(*_WRITE_ROLES))],
    store: Annotated[AgentRegistryStore, Depends(get_registry_store)],
    iac_generator: Annotated[IaCGenerator, Depends(get_iac_generator)],
    git_provider: Annotated[GitProvider, Depends(get_git_provider)],
    deployment_orchestrator: Annotated[
        DeploymentOrchestrator, Depends(get_deployment_orchestrator)
    ],
    deployment_status_store: Annotated[DeploymentStatusStore, Depends(get_deployment_status_store)],
    platform_settings_store: Annotated[PlatformSettingsStore, Depends(get_platform_settings_store)],
    audit_writer: Annotated[AuditWriter, Depends(get_audit_writer)],
    metrics_emitter: Annotated[MetricsEmitter, Depends(get_metrics_emitter)],
    pipeline_simulator: Annotated[DeploymentPipelineSimulator, Depends(get_pipeline_simulator)],
    security_audit_log_store: Annotated[
        SecurityAuditLogStore, Depends(get_security_audit_log_store)
    ],
    background_tasks: BackgroundTasks,
) -> RollbackResponse:
    try:
        record, new_version = await store.rollback_agent(
            tenant_id=tenant_id,
            agent_id=agent_id,
            target_version=payload.target_version,
            reason=payload.reason,
            changed_by=current_user.email,
        )
    except AgentNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except VersionNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except InvalidRollbackError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    except CircularDependencyError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc

    # store.rollback_agent always sets this from the pre-rollback current_version.
    assert new_version.rolled_back_from_version is not None

    triggered = await _trigger_deployment(
        tenant_id=tenant_id,
        agent_id=agent_id,
        version=record.current_version,
        configuration=new_version.configuration,
        triggered_by=current_user.email,
        store=store,
        iac_generator=iac_generator,
        git_provider=git_provider,
        deployment_orchestrator=deployment_orchestrator,
        deployment_status_store=deployment_status_store,
        platform_settings_store=platform_settings_store,
        background_tasks=background_tasks,
        pipeline_simulator=pipeline_simulator,
    )

    await _record_event(
        audit_writer=audit_writer,
        metrics_emitter=metrics_emitter,
        event_type="rollback",
        metric_name="AgentRolledBack",
        tenant_id=tenant_id,
        agent_id=agent_id,
        actor=current_user.email,
        summary=(
            f"Agent {agent_id!r} rolled back to v{payload.target_version} "
            f"(new v{triggered.updated_record.current_version}): {payload.reason}"
        ),
        security_audit_log_store=security_audit_log_store,
        security_event_type="agent.version_rolled_back",
        security_action="rollback",
    )

    return RollbackResponse(
        agent_id=triggered.updated_record.agent_id,
        version=triggered.updated_record.current_version,
        status=triggered.updated_record.status,
        rolled_back_from_version=new_version.rolled_back_from_version,
        updated_at=triggered.updated_record.updated_at,
        deployment_id=triggered.deployment_id,
        branch=triggered.branch,
        pull_request_id=triggered.pull_request_id,
    )


@router.post("/{agent_id}/generate-iac", response_model=GenerateIaCResponse)
async def generate_iac(
    agent_id: str,
    tenant_id: Annotated[str, Depends(get_tenant_id)],
    current_user: Annotated[CurrentUser, Depends(require_role(*_WRITE_ROLES))],
    store: Annotated[AgentRegistryStore, Depends(get_registry_store)],
    iac_generator: Annotated[IaCGenerator, Depends(get_iac_generator)],
    iac_validator: Annotated[IaCValidator, Depends(get_iac_validator)],
    validation_mode: TerraformValidationMode = "local",
) -> GenerateIaCResponse:
    # Development Terraform Validation Mode: "local" (default) always runs —
    # it never requires AWS credentials or contacts a real AWS account
    # (IaCValidator uses `terraform init -backend=false` only). The
    # "panasa_vpc"/"customer_vpc" modes are admin/developer-only
    # placeholders for later stages (Section 35 Stage 2/3) — hidden unless
    # explicitly enabled, and never perform a real deployment even when
    # enabled (Stage 1 scope).
    if validation_mode != "local":
        if not settings.dev_validation_extended_modes_enabled:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=(
                    f"Validation mode {validation_mode!r} is disabled on this deployment. "
                    "Only 'local' validation is available."
                ),
            )
        if current_user.role not in ("developer", "admin"):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=(
                    f"Validation mode {validation_mode!r} requires the developer " "or admin role."
                ),
            )

    record = await store.get_agent(tenant_id, agent_id)
    if record is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"Agent {agent_id!r} not found"
        )

    version_record = await store.get_version(agent_id, record.current_version)
    if version_record is None:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Current version {record.current_version} missing for agent {agent_id!r}",
        )

    result = await iac_generator.generate(
        agent_id=agent_id,
        tenant_id=tenant_id,
        version=record.current_version,
        config=version_record.configuration,
    )
    validation_report = await iac_validator.validate(
        agent_id=agent_id,
        tenant_id=tenant_id,
        version=record.current_version,
        config=version_record.configuration,
        files=result.files,
        tool=result.tool,
    )
    # R40: persisted either way (a failed report is exactly what a developer
    # needs to look up later — "why did v3's IaC fail last Tuesday"), but
    # never handed to the caller as part of a 200/success response. No
    # partially-validated bundle is ever returned as usable.
    await store.record_iac_artifact(
        tenant_id=tenant_id,
        agent_id=agent_id,
        version=record.current_version,
        iac_version=result.iac_version,
        iac_s3_key=result.s3_key,
        iac_modules=result.modules,
        iac_validation_report=validation_report,
    )

    if not validation_report.passed:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=validation_report.model_dump(mode="json"),
        )

    environment_note = None
    if validation_mode != "local":
        environment_note = (
            f"Real deployment to {validation_mode!r} is not implemented in Stage 1. "
            "This response reflects local generation and validation only — "
            "no AWS account was contacted."
        )

    return GenerateIaCResponse(
        agent_id=agent_id,
        version=record.current_version,
        tool=result.tool,
        iac_version=result.iac_version,
        s3_key=result.s3_key,
        modules=result.modules,
        validation_report=validation_report,
        validation_mode=validation_mode,
        environment_note=environment_note,
    )


@router.get("/{agent_id}/iac/status", response_model=IaCStatusResponse)
async def get_iac_status(
    agent_id: str,
    tenant_id: Annotated[str, Depends(get_tenant_id)],
    _current_user: Annotated[CurrentUser, Depends(require_role(*_READ_ROLES))],
    store: Annotated[AgentRegistryStore, Depends(get_registry_store)],
) -> IaCStatusResponse:
    """Wizard Redesign QA A-04/U-08 — the UI's IaC generation progress panel
    polls this. See IaCStatusResponse's docstring: generate-iac has already
    completed by the time this is ever polled (no background job to
    observe mid-flight), so every stage in `modules` is reported
    "completed"/"pending" from the already-persisted result of the most
    recent generate-iac call, not a live in-progress state."""
    record = await store.get_agent(tenant_id, agent_id)
    if record is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"Agent {agent_id!r} not found"
        )
    version_record = await store.get_version(agent_id, record.current_version)
    if version_record is None:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Current version {record.current_version} missing for agent {agent_id!r}",
        )

    if version_record.iac_version is None:
        return IaCStatusResponse(
            agent_id=agent_id, version=record.current_version, status="not_started", stages=[]
        )

    modules = version_record.iac_modules or []
    report = version_record.iac_validation_report
    overall_status: Literal["completed", "failed"] = (
        "completed" if report is not None and report.passed else "failed"
    )
    return IaCStatusResponse(
        agent_id=agent_id,
        version=record.current_version,
        status=overall_status,
        stages=[IaCStageStatus(name=m, status="completed") for m in modules],
        validation=report,
    )


@router.post("/{agent_id}/deploy", response_model=DeployResponse)
async def deploy_agent(
    agent_id: str,
    tenant_id: Annotated[str, Depends(get_tenant_id)],
    current_user: Annotated[CurrentUser, Depends(require_role(*_WRITE_ROLES))],
    store: Annotated[AgentRegistryStore, Depends(get_registry_store)],
    iac_generator: Annotated[IaCGenerator, Depends(get_iac_generator)],
    git_provider: Annotated[GitProvider, Depends(get_git_provider)],
    deployment_orchestrator: Annotated[
        DeploymentOrchestrator, Depends(get_deployment_orchestrator)
    ],
    deployment_status_store: Annotated[DeploymentStatusStore, Depends(get_deployment_status_store)],
    platform_settings_store: Annotated[PlatformSettingsStore, Depends(get_platform_settings_store)],
    audit_writer: Annotated[AuditWriter, Depends(get_audit_writer)],
    metrics_emitter: Annotated[MetricsEmitter, Depends(get_metrics_emitter)],
    pipeline_simulator: Annotated[DeploymentPipelineSimulator, Depends(get_pipeline_simulator)],
    background_tasks: BackgroundTasks,
) -> DeployResponse:
    record = await store.get_agent(tenant_id, agent_id)
    if record is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"Agent {agent_id!r} not found"
        )

    version_record = await store.get_version(agent_id, record.current_version)
    if version_record is None:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Current version {record.current_version} missing for agent {agent_id!r}",
        )

    triggered = await _trigger_deployment(
        tenant_id=tenant_id,
        agent_id=agent_id,
        version=record.current_version,
        configuration=version_record.configuration,
        triggered_by=current_user.email,
        store=store,
        iac_generator=iac_generator,
        git_provider=git_provider,
        deployment_orchestrator=deployment_orchestrator,
        deployment_status_store=deployment_status_store,
        platform_settings_store=platform_settings_store,
        background_tasks=background_tasks,
        pipeline_simulator=pipeline_simulator,
    )

    await _record_event(
        audit_writer=audit_writer,
        metrics_emitter=metrics_emitter,
        event_type="deploy",
        metric_name="AgentDeployTriggered",
        tenant_id=tenant_id,
        agent_id=agent_id,
        actor=current_user.email,
        summary=(
            f"Agent {agent_id!r} v{record.current_version} "
            f"deployment {triggered.deployment_id} triggered"
        ),
    )

    return DeployResponse(
        agent_id=agent_id,
        version=record.current_version,
        deployment_id=triggered.deployment_id,
        status=triggered.updated_record.status,
        branch=triggered.branch,
        pull_request_id=triggered.pull_request_id,
    )


@router.get("/{agent_id}/deployments", response_model=DeploymentListResponse)
async def list_deployments(
    agent_id: str,
    tenant_id: Annotated[str, Depends(get_tenant_id)],
    _current_user: Annotated[CurrentUser, Depends(require_role(*_READ_ROLES))],
    store: Annotated[AgentRegistryStore, Depends(get_registry_store)],
    deployment_status_store: Annotated[DeploymentStatusStore, Depends(get_deployment_status_store)],
) -> DeploymentListResponse:
    if await store.get_agent(tenant_id, agent_id) is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"Agent {agent_id!r} not found"
        )

    deployments = await deployment_status_store.list_deployments(agent_id)
    return DeploymentListResponse(items=deployments)


@router.post("/{agent_id}/deployments/{deployment_id}/approve", response_model=DeploymentRecord)
async def approve_deployment(
    agent_id: str,
    deployment_id: str,
    tenant_id: Annotated[str, Depends(get_tenant_id)],
    current_user: Annotated[CurrentUser, Depends(require_role(*_WRITE_ROLES))],
    store: Annotated[AgentRegistryStore, Depends(get_registry_store)],
    deployment_status_store: Annotated[DeploymentStatusStore, Depends(get_deployment_status_store)],
    deployment_orchestrator: Annotated[
        DeploymentOrchestrator, Depends(get_deployment_orchestrator)
    ],
    pipeline_simulator: Annotated[DeploymentPipelineSimulator, Depends(get_pipeline_simulator)],
    background_tasks: BackgroundTasks,
) -> DeploymentRecord:
    """Section 45.4/R50 (resolved as configurable — see
    deployment/models.py's module docstring): approve a "manual"-mode
    deployment parked at PENDING_APPROVAL. A no-op deployment_id under the
    default "automated" mode (F1/R06) never reaches PENDING_APPROVAL in the
    first place — POLICY_CHECK already decided PASS/BLOCK on its own — so
    calling this here is a 409, not a silent success: there is nothing for
    a human to approve.
    """
    if await store.get_agent(tenant_id, agent_id) is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"Agent {agent_id!r} not found"
        )

    record = await deployment_status_store.get_deployment(agent_id, deployment_id)
    if record is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Deployment {deployment_id!r} not found for agent {agent_id!r}",
        )

    if record.approval_mode != "manual":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "error": "approval_not_applicable",
                "message": (
                    f"Deployment {deployment_id!r} runs the automated pipeline "
                    "(approval_mode='automated') — POLICY_CHECK already decided "
                    "PASS/BLOCK automatically. There is nothing to approve."
                ),
            },
        )

    if record.status != "PENDING_APPROVAL":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "error": "not_pending_approval",
                "message": (
                    f"Deployment {deployment_id!r} is in status {record.status!r}, "
                    "not PENDING_APPROVAL."
                ),
            },
        )

    # status alone doesn't change on approval (only the customer's CI/CD
    # moves PENDING_APPROVAL -> APPLYING, per this module's docstring) — so
    # a second call would otherwise silently re-approve the same deployment.
    if record.approved_by is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "error": "already_approved",
                "message": (
                    f"Deployment {deployment_id!r} was already approved by "
                    f"{record.approved_by!r} at {record.approved_at}."
                ),
            },
        )

    updated_record = await deployment_status_store.record_approval(
        agent_id, deployment_id, approved_by=current_user.email
    )
    await deployment_orchestrator.notify_deployment_approved(
        agent_id, deployment_id, tenant_id, approved_by=current_user.email
    )
    if settings.simulate_deployment_pipeline:
        background_tasks.add_task(
            pipeline_simulator.resume_after_approval, tenant_id, agent_id, deployment_id
        )
    return updated_record


@router.post("/{agent_id}/credentials/rotate", response_model=RotateApiKeyResponse)
async def rotate_agent_api_key(
    agent_id: str,
    tenant_id: Annotated[str, Depends(get_tenant_id)],
    current_user: Annotated[CurrentUser, Depends(require_role(*_WRITE_ROLES))],
    store: Annotated[AgentRegistryStore, Depends(get_registry_store)],
    secrets_manager: Annotated[SecretsManager, Depends(get_secrets_manager)],
    audit_writer: Annotated[AuditWriter, Depends(get_audit_writer)],
    metrics_emitter: Annotated[MetricsEmitter, Depends(get_metrics_emitter)],
    security_audit_log_store: Annotated[
        SecurityAuditLogStore, Depends(get_security_audit_log_store)
    ],
) -> RotateApiKeyResponse:
    """S-02 dual-key rotation (CLAUDE.md Section 64.4). Generates a brand
    new Secrets Manager secret (never reuses/overwrites the old ARN in
    place) so both the old and new value can validate simultaneously
    during the 24h grace window — services/agent-runtime/auth.py's
    ApiKeyAuthProvider (Phase 1) already implements that dual-key check.

    Enterprise-mode agents are never rotated here — R60: their key is
    Terraform-generated inside the customer VPC, this Runtime has no way
    to reach it (a real reveal/rotate for those is customer CI/CD's job,
    tracked separately under DEP-INF-01/02, Section 59/60 — out of this
    Sprint 3 security phase)."""
    if settings.deployment_mode != "prototype":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                "API key rotation for enterprise-mode agents is managed by your own "
                "Terraform/CI-CD (R60) — not available via this endpoint."
            ),
        )

    raw_api_key = _generate_api_key()
    new_secret_name = f"panasa-{agent_id}-api-key-{int(time.time())}"
    new_api_key_secret_arn = await secrets_manager.create_secret(new_secret_name, raw_api_key)
    # 24h dual-key grace window (Section 64.4) — epoch seconds, matching
    # services/agent-runtime/auth.py's `time.time() < previous_key_expires_at`.
    previous_key_expires_at = int(time.time()) + (24 * 3600)

    try:
        record = await store.rotate_api_key(
            tenant_id=tenant_id,
            agent_id=agent_id,
            new_api_key_secret_arn=new_api_key_secret_arn,
            previous_key_expires_at=previous_key_expires_at,
            updated_by=current_user.email,
        )
    except AgentNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except NoApiKeyProvisionedError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc

    assert record.rotated_at is not None  # rotate_api_key always sets this
    await _record_event(
        audit_writer=audit_writer,
        metrics_emitter=metrics_emitter,
        event_type="config_change",
        metric_name="AgentCredentialRotated",
        tenant_id=tenant_id,
        agent_id=agent_id,
        actor=current_user.email,
        summary=f"API key rotated for agent {agent_id!r}",
        security_audit_log_store=security_audit_log_store,
        security_event_type="credential.rotated",
        security_action="rotate",
    )

    return RotateApiKeyResponse(
        agent_id=agent_id,
        api_key_secret_arn=new_api_key_secret_arn,
        api_key=raw_api_key,
        previous_key_valid_until=datetime.fromtimestamp(previous_key_expires_at, UTC).isoformat(),
        rotated_at=record.rotated_at,
    )


@router.post("/{agent_id}/credentials/revoke", response_model=RevokeApiKeyResponse)
async def revoke_agent_api_key(
    agent_id: str,
    tenant_id: Annotated[str, Depends(get_tenant_id)],
    current_user: Annotated[CurrentUser, Depends(require_role(*_WRITE_ROLES))],
    store: Annotated[AgentRegistryStore, Depends(get_registry_store)],
    audit_writer: Annotated[AuditWriter, Depends(get_audit_writer)],
    metrics_emitter: Annotated[MetricsEmitter, Depends(get_metrics_emitter)],
    security_audit_log_store: Annotated[
        SecurityAuditLogStore, Depends(get_security_audit_log_store)
    ],
) -> RevokeApiKeyResponse:
    """S-02 emergency revocation — denies both the current and any
    in-grace-window previous key immediately (services/agent-runtime/
    auth.py checks api_key_revoked before comparing any secret value, so
    revocation takes effect without waiting on the 5-minute value-cache
    TTL). Idempotent: revoking an already-revoked agent is a no-op
    success. There is no un-revoke endpoint — CLAUDE.md's own S-02 scope
    and Section 61.2 audit taxonomy name only credential.created/rotated/
    revoked, not a reactivation event; recovery is a fresh rotate."""
    try:
        record = await store.revoke_api_key(
            tenant_id=tenant_id, agent_id=agent_id, updated_by=current_user.email
        )
    except AgentNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc

    await _record_event(
        audit_writer=audit_writer,
        metrics_emitter=metrics_emitter,
        event_type="config_change",
        metric_name="AgentCredentialRevoked",
        tenant_id=tenant_id,
        agent_id=agent_id,
        actor=current_user.email,
        summary=f"API key revoked for agent {agent_id!r}",
        security_audit_log_store=security_audit_log_store,
        security_event_type="credential.revoked",
        security_action="revoke",
    )

    return RevokeApiKeyResponse(
        agent_id=agent_id,
        api_key_revoked=record.api_key_revoked,
        updated_at=record.updated_at,
    )


@router.put("/{agent_id}/credentials/jwt-config", response_model=JwtConfigResponse)
async def set_agent_jwt_config(
    agent_id: str,
    request: SetJwtConfigRequest,
    tenant_id: Annotated[str, Depends(get_tenant_id)],
    current_user: Annotated[CurrentUser, Depends(require_role(*_WRITE_ROLES))],
    store: Annotated[AgentRegistryStore, Depends(get_registry_store)],
    audit_writer: Annotated[AuditWriter, Depends(get_audit_writer)],
    metrics_emitter: Annotated[MetricsEmitter, Depends(get_metrics_emitter)],
    security_audit_log_store: Annotated[
        SecurityAuditLogStore, Depends(get_security_audit_log_store)
    ],
) -> JwtConfigResponse:
    """S-12 (CLAUDE.md Section 63.1) — configures this agent's JWT/OAuth2
    trust for services/agent-runtime/auth.py's JwtAuthProvider: the
    CUSTOMER's own identity provider, a completely separate trust root
    from this Runtime's own Factory Console user auth. No UI wizard step
    exists for this yet (API-only) — same "backend wired, no wizard form"
    gap already flagged for S-03's quota fields, Section 67.4."""
    if bool(request.jwt_issuer) != bool(request.jwt_jwks_url):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="jwt_issuer and jwt_jwks_url must be set together, or both left unset.",
        )
    if request.jwt_jwks_url is not None and not request.jwt_jwks_url.startswith("https://"):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="jwt_jwks_url must be an https:// URL.",
        )

    try:
        record = await store.set_jwt_config(
            tenant_id=tenant_id,
            agent_id=agent_id,
            jwt_issuer=request.jwt_issuer,
            jwt_audience=request.jwt_audience,
            jwt_jwks_url=request.jwt_jwks_url,
            jwt_tenant_claim=request.jwt_tenant_claim,
            updated_by=current_user.email,
        )
    except AgentNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc

    await _record_event(
        audit_writer=audit_writer,
        metrics_emitter=metrics_emitter,
        event_type="config_change",
        metric_name="AgentJwtConfigUpdated",
        tenant_id=tenant_id,
        agent_id=agent_id,
        actor=current_user.email,
        summary=f"JWT auth config updated for agent {agent_id!r}",
        security_audit_log_store=security_audit_log_store,
        security_event_type="agent.updated",
        security_action="update",
    )

    return JwtConfigResponse(
        agent_id=agent_id,
        jwt_issuer=record.jwt_issuer,
        jwt_audience=record.jwt_audience,
        jwt_jwks_url=record.jwt_jwks_url,
        jwt_tenant_claim=record.jwt_tenant_claim,
        updated_at=record.updated_at,
    )


@router.post("/{agent_id}/integration/reveal-api-key", response_model=RevealApiKeyResponse)
async def reveal_api_key(
    agent_id: str,
    request: Request,
    tenant_id: Annotated[str, Depends(get_tenant_id)],
    current_user: Annotated[CurrentUser, Depends(require_role(*_WRITE_ROLES))],
    store: Annotated[AgentRegistryStore, Depends(get_registry_store)],
    secrets_manager: Annotated[SecretsManager, Depends(get_secrets_manager)],
    redis_client: Annotated[redis.Redis, Depends(get_redis_client)],
    audit_writer: Annotated[AuditWriter, Depends(get_audit_writer)],
    metrics_emitter: Annotated[MetricsEmitter, Depends(get_metrics_emitter)],
    security_audit_log_store: Annotated[
        SecurityAuditLogStore, Depends(get_security_audit_log_store)
    ],
) -> RevealApiKeyResponse:
    """Sprint 4 Phase 1 (S-06, CLAUDE.md Section 56.7/57, R60-R63) — shows
    the raw current API key value again after creation/rotation, for a
    caller who lost it. Prototype mode only (R62): enterprise-mode agents
    have no key for this Runtime to reveal at all — Terraform's
    random_password generates it inside the customer's own VPC (R60), and
    the business app retrieves it from the customer's own Secrets Manager,
    never through this endpoint.

    Rate-limited to 3 reveals/hour/agent (R63's own hardening — a leaked
    key is really addressed by rotate/revoke, not by slowing this down,
    but the limit still makes credential-stuffing-style probing of this
    endpoint impractical) and every call is audit-logged with source IP
    BEFORE the key is returned, whether or not the call ultimately
    succeeds past that point — matching R63's "every call... is recorded"
    wording exactly (not just successful ones)."""
    if settings.deployment_mode != "prototype":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                "API key reveal is not available for enterprise-mode agents (R62) — "
                "the key is managed entirely in your own AWS account's Secrets Manager."
            ),
        )

    source_ip = request.client.host if request.client else ""

    record = await store.get_agent(tenant_id, agent_id)
    if record is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"Agent {agent_id!r} not found"
        )

    await _record_event(
        audit_writer=audit_writer,
        metrics_emitter=metrics_emitter,
        event_type="config_change",
        metric_name="AgentCredentialRevealed",
        tenant_id=tenant_id,
        agent_id=agent_id,
        actor=current_user.email,
        summary=f"API key reveal requested for agent {agent_id!r}",
        security_audit_log_store=security_audit_log_store,
        security_event_type="credential.revealed",
        security_action="reveal",
        source_ip=source_ip,
    )

    if record.api_key_secret_arn is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Agent {agent_id!r} has no API key provisioned to reveal",
        )

    allowed = await check_reveal_rate_limit(tenant_id, agent_id, redis_client)
    if not allowed:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Reveal rate limit exceeded. Max 3 per hour.",
        )

    try:
        api_key = await secrets_manager.get_secret_value(record.api_key_secret_arn)
    except SecretNotFoundError as exc:
        # The ARN is on the record but the secret itself is gone — a
        # data-integrity problem this Runtime can't self-heal from, not a
        # client error.
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="This agent's API key secret could not be found",
        ) from exc

    return RevealApiKeyResponse(agent_id=agent_id, api_key=api_key)
