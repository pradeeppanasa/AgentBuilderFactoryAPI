"""Agents — CRUD + lifecycle + versioning (CLAUDE.md Section 5.1/5.2).

R01: tenant_id (via get_tenant_id) is threaded through every store call.
R08: PUT/rollback never overwrite a version — they always create a new one.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel

from app.config import settings
from app.dependencies import (
    get_audit_writer,
    get_deployment_orchestrator,
    get_deployment_status_store,
    get_git_provider,
    get_iac_generator,
    get_metrics_emitter,
    get_registry_store,
    get_tenant_id,
)
from app.modules.audit.writer import AuditEvent, AuditWriter
from app.modules.auth.dependencies import require_role
from app.modules.auth.schemas import CurrentUser
from app.modules.change_impact.analyzer import ChangeImpactAnalyzer, ImpactAnalysis
from app.modules.deployment.models import DeploymentRecord, initial_stages
from app.modules.deployment.orchestrator import DeploymentOrchestrator
from app.modules.deployment.status_store import DeploymentStatusStore
from app.modules.git_provider.base import GitProvider
from app.modules.iac_generator.generator import IaCGenerator
from app.modules.observability.metrics import MetricsEmitter
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
    pull_request_id: str


class GenerateIaCResponse(BaseModel):
    agent_id: str
    version: int
    tool: str
    iac_version: str
    s3_key: str
    modules: list[str]


class DeployResponse(BaseModel):
    agent_id: str
    version: int
    deployment_id: str
    status: AgentStatus
    branch: str
    pull_request_id: str


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
    pull_request_id: str
    updated_record: AgentRecord


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
) -> _TriggeredDeployment:
    """Shared by deploy_agent and rollback_agent (Phase 13: "Rollback
    endpoint creates new version from old config, triggers deployment") —
    generate IaC, open the F5 PR, publish the EventBridge event, and record
    the trigger. R22: the version being replaced stays LIVE throughout;
    nothing here touches live_version — only MarkActive (Phase 11) does,
    once HEALTH_CHECK passes.
    """
    if not settings.git_repo_url:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="GIT_REPO_URL is not configured",
        )

    deployment_id = f"DEP-{uuid.uuid4().hex[:8].upper()}"
    branch = f"panasa/agent-{agent_id}-v{version}-{deployment_id}"

    iac_result = await iac_generator.generate(
        agent_id=agent_id, version=version, config=configuration
    )
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
            stages=initial_stages(),
            iac_s3_key=iac_result.s3_key,
            updated_at=now,
        )
    )

    await git_provider.create_branch(settings.git_repo_url, branch)
    await git_provider.commit_files(
        settings.git_repo_url,
        branch,
        iac_result.files,
        message=f"Agent {agent_id} v{version} — generated {iac_result.tool} IaC",
    )
    pull_request_id = await git_provider.create_pull_request(
        settings.git_repo_url,
        branch,
        title=f"[Panasa Auto] {agent_id} v{version} — Deploy",
        description=(
            f"Agent: {agent_id} | Version: {version} | Impact: PENDING\n"
            "Security: pending | RAGAS: pending"
        ),
    )

    await deployment_orchestrator.trigger_deployment(
        agent_id=agent_id, version=version, deployment_id=deployment_id, tenant_id=tenant_id
    )

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
    _current_user: Annotated[CurrentUser, Depends(require_role(*_WRITE_ROLES))],
    store: Annotated[AgentRegistryStore, Depends(get_registry_store)],
    iac_generator: Annotated[IaCGenerator, Depends(get_iac_generator)],
) -> GenerateIaCResponse:
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
        version=record.current_version,
        config=version_record.configuration,
    )
    await store.record_iac_artifact(
        tenant_id=tenant_id,
        agent_id=agent_id,
        version=record.current_version,
        iac_version=result.iac_version,
        iac_s3_key=result.s3_key,
    )

    return GenerateIaCResponse(
        agent_id=agent_id,
        version=record.current_version,
        tool=result.tool,
        iac_version=result.iac_version,
        s3_key=result.s3_key,
        modules=result.modules,
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
