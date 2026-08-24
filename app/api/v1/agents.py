"""Agents — CRUD + lifecycle + versioning (CLAUDE.md Section 5.1/5.2).

R01: tenant_id (via get_tenant_id) is threaded through every store call.
R08: PUT/rollback never overwrite a version — they always create a new one.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Annotated, Literal

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field

from app.config import settings
from app.dependencies import (
    get_audit_writer,
    get_deployment_orchestrator,
    get_deployment_status_store,
    get_git_provider,
    get_iac_generator,
    get_iac_validator,
    get_metrics_emitter,
    get_platform_settings_store,
    get_registry_store,
    get_tenant_id,
)
from app.modules.audit.writer import AuditEvent, AuditWriter
from app.modules.auth.dependencies import require_role
from app.modules.auth.schemas import CurrentUser
from app.modules.change_impact.analyzer import ChangeImpactAnalyzer, ImpactAnalysis
from app.modules.deployment.models import ApprovalMode, DeploymentRecord, initial_stages
from app.modules.deployment.orchestrator import DeploymentOrchestrator
from app.modules.deployment.status_store import DeploymentStatusStore
from app.modules.git_provider._util import agent_repo_identifier
from app.modules.git_provider.base import GitProvider
from app.modules.iac_generator.cicd_templates import generate_cicd_workflow
from app.modules.iac_generator.generator import IaCGenerator
from app.modules.iac_generator.validation_models import (
    IaCValidationReport,
    TerraformValidationMode,
)
from app.modules.iac_generator.validator import IaCValidator
from app.modules.observability.metrics import MetricsEmitter
from app.modules.platform_settings.store import PlatformSettingsStore
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
from app.shared.exceptions import (
    AgentNotFoundError,
    CircularDependencyError,
    InvalidRollbackError,
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
) -> None:
    """Phase 14: one audit event + one CloudWatch metric per key operation
    (config_change, deploy, rollback here; block lives in
    app.modules.security.policy_enforcement — see that module for why
    guardrail_decision/tool_call have no call site in this Runtime at all).
    Both are fire-and-forget/best-effort by design (see their own modules'
    docstrings) — neither can fail the request that triggered them.
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


@router.post("", response_model=CreateAgentResponse, status_code=status.HTTP_201_CREATED)
async def create_agent(
    payload: CreateAgentRequest,
    tenant_id: Annotated[str, Depends(get_tenant_id)],
    current_user: Annotated[CurrentUser, Depends(require_role(*_WRITE_ROLES))],
    store: Annotated[AgentRegistryStore, Depends(get_registry_store)],
    audit_writer: Annotated[AuditWriter, Depends(get_audit_writer)],
    metrics_emitter: Annotated[MetricsEmitter, Depends(get_metrics_emitter)],
) -> CreateAgentResponse:
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
    )

    return CreateAgentResponse(
        agent_id=record.agent_id,
        version=record.current_version,
        status=record.status,
        created_at=record.created_at,
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
) -> UpdateAgentResponse:
    try:
        record, _version = await store.update_agent(
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
        if not repo_already_existed:
            # Section 45.6/R58 — the workflow file is a per-repo artifact,
            # committed once alongside the repo's very first Terraform.
            # Changing the tenant's cicd_provider/approval_mode later never
            # rewrites an already-created repo's workflow file (same
            # "read once, at creation" rule as approval_mode itself — see
            # PlatformSettingsRecord.cicd_provider's docstring).
            workflow_path, workflow_content = generate_cicd_workflow(
                tenant_settings.cicd_provider, approval_mode
            )
            files[workflow_path] = workflow_content

        pull_request_id: str | None
        if repo_already_existed:
            branch = f"deploy/v{version}-{deployment_id}"
            await git_provider.create_branch(repo, branch, from_branch=settings.git_default_branch)
            await git_provider.commit_files(
                repo,
                branch,
                files,
                message=f"Agent {agent_id} v{version} — generated {iac_result.tool} IaC",
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
            )
            pull_request_id = None
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
                    f"Validation mode {validation_mode!r} requires the developer "
                    "or admin role."
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
    return updated_record
