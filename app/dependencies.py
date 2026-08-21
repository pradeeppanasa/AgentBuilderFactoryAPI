"""Shared FastAPI dependencies (auth, db, aws).

`get_tenant_id` derives tenant_id from the authenticated user's JWT claims
(R01: tenant_id is required on every DynamoDB operation). Route handlers
depend on this function by name, not on how it's implemented — it used to
read an X-Tenant-Id header as a Phase 1/2 placeholder; Phase 3 wires it to
the real source without any route signature changes.
"""

from __future__ import annotations

from typing import Annotated, Any

import redis.asyncio as redis
from fastapi import Depends, Request

from app.modules.audit.writer import AuditWriter
from app.modules.auth.dependencies import get_current_user
from app.modules.auth.schemas import CurrentUser
from app.modules.bedrock_credentials.store import BedrockCredentialStore
from app.modules.connectors.catalog import ConnectorCatalogStore
from app.modules.connectors.tester import ConnectorTester
from app.modules.deployment.orchestrator import DeploymentOrchestrator
from app.modules.deployment.status_store import DeploymentStatusStore
from app.modules.git_provider.base import GitProvider
from app.modules.guardrails.engine import GuardrailEngine
from app.modules.guardrails.provisioner import BedrockGuardrailProvisioner
from app.modules.guardrails.store import GuardrailPolicyStore
from app.modules.hitl.store import HitlReviewStore
from app.modules.iac_generator.generator import IaCGenerator
from app.modules.iac_generator.validator import IaCValidator
from app.modules.knowledge_base.provisioner import BedrockKnowledgeBaseProvisioner
from app.modules.knowledge_base.store import KnowledgeBaseStore
from app.modules.observability.metrics import MetricsEmitter
from app.modules.platform.upgrade_orchestrator import PlatformUpgradeOrchestrator
from app.modules.platform.upgrade_store import PlatformUpgradeStatusStore
from app.modules.platform.version_service import PlatformVersionService
from app.modules.platform_settings.store import PlatformSettingsStore
from app.modules.playground.store import PlaygroundSessionStore
from app.modules.projects.store import ProjectStore
from app.modules.registry.store import AgentRegistryStore
from app.modules.runs.store import RunStore
from app.modules.secrets.manager import SecretsManager
from app.modules.skills.store import SkillStore
from app.modules.task_planner.session_store import BuildWithAISessionStore
from app.modules.telemetry.emitter import TelemetryConfig, TelemetryEmitter


async def get_tenant_id(current_user: Annotated[CurrentUser, Depends(get_current_user)]) -> str:
    return current_user.tenant_id


async def get_registry_store(request: Request) -> AgentRegistryStore:
    store: AgentRegistryStore = request.app.state.registry_store
    return store


async def get_iac_generator(request: Request) -> IaCGenerator:
    generator: IaCGenerator = request.app.state.iac_generator
    return generator


async def get_iac_validator(request: Request) -> IaCValidator:
    validator: IaCValidator = request.app.state.iac_validator
    return validator


async def get_git_provider(request: Request) -> GitProvider:
    provider: GitProvider = request.app.state.git_provider
    return provider


async def get_deployment_orchestrator(request: Request) -> DeploymentOrchestrator:
    orchestrator: DeploymentOrchestrator = request.app.state.deployment_orchestrator
    return orchestrator


async def get_deployment_status_store(request: Request) -> DeploymentStatusStore:
    store: DeploymentStatusStore = request.app.state.deployment_status_store
    return store


async def get_redis_client(request: Request) -> redis.Redis:
    client: redis.Redis = request.app.state.redis_client
    return client


async def get_dynamodb_resource(request: Request) -> Any:
    return request.app.state.dynamodb


async def get_s3_client(request: Request) -> Any:
    return request.app.state.s3_client


async def get_connector_catalog_store(request: Request) -> ConnectorCatalogStore:
    store: ConnectorCatalogStore = request.app.state.connector_catalog_store
    return store


async def get_secrets_manager(request: Request) -> SecretsManager:
    manager: SecretsManager = request.app.state.secrets_manager
    return manager


async def get_connector_tester(request: Request) -> ConnectorTester:
    tester: ConnectorTester = request.app.state.connector_tester
    return tester


async def get_metrics_emitter(request: Request) -> MetricsEmitter:
    emitter: MetricsEmitter = request.app.state.metrics_emitter
    return emitter


async def get_audit_writer(request: Request) -> AuditWriter:
    writer: AuditWriter = request.app.state.audit_writer
    return writer


async def get_platform_version_service(request: Request) -> PlatformVersionService:
    service: PlatformVersionService = request.app.state.platform_version_service
    return service


async def get_platform_upgrade_store(request: Request) -> PlatformUpgradeStatusStore:
    store: PlatformUpgradeStatusStore = request.app.state.platform_upgrade_store
    return store


async def get_platform_upgrade_orchestrator(request: Request) -> PlatformUpgradeOrchestrator:
    orchestrator: PlatformUpgradeOrchestrator = request.app.state.platform_upgrade_orchestrator
    return orchestrator


async def get_telemetry_config(request: Request) -> TelemetryConfig:
    config: TelemetryConfig = request.app.state.telemetry_config
    return config


async def get_telemetry_emitter(request: Request) -> TelemetryEmitter:
    emitter: TelemetryEmitter = request.app.state.telemetry_emitter
    return emitter


async def get_knowledge_base_store(request: Request) -> KnowledgeBaseStore:
    store: KnowledgeBaseStore = request.app.state.knowledge_base_store
    return store


async def get_bedrock_kb_provisioner(request: Request) -> BedrockKnowledgeBaseProvisioner:
    provisioner: BedrockKnowledgeBaseProvisioner = request.app.state.bedrock_kb_provisioner
    return provisioner


async def get_guardrail_policy_store(request: Request) -> GuardrailPolicyStore:
    store: GuardrailPolicyStore = request.app.state.guardrail_policy_store
    return store


async def get_guardrail_engine(request: Request) -> GuardrailEngine:
    engine: GuardrailEngine = request.app.state.guardrail_engine
    return engine


async def get_bedrock_guardrail_provisioner(request: Request) -> BedrockGuardrailProvisioner:
    provisioner: BedrockGuardrailProvisioner = request.app.state.bedrock_guardrail_provisioner
    return provisioner


async def get_playground_session_store(request: Request) -> PlaygroundSessionStore:
    store: PlaygroundSessionStore = request.app.state.playground_session_store
    return store


async def get_build_with_ai_session_store(request: Request) -> BuildWithAISessionStore:
    store: BuildWithAISessionStore = request.app.state.build_with_ai_session_store
    return store


async def get_run_store(request: Request) -> RunStore:
    store: RunStore = request.app.state.run_store
    return store


async def get_bedrock_credential_store(request: Request) -> BedrockCredentialStore:
    store: BedrockCredentialStore = request.app.state.bedrock_credential_store
    return store


async def get_project_store(request: Request) -> ProjectStore:
    store: ProjectStore = request.app.state.project_store
    return store


async def get_skill_store(request: Request) -> SkillStore:
    store: SkillStore = request.app.state.skill_store
    return store


async def get_hitl_review_store(request: Request) -> HitlReviewStore:
    store: HitlReviewStore = request.app.state.hitl_review_store
    return store


async def get_platform_settings_store(request: Request) -> PlatformSettingsStore:
    store: PlatformSettingsStore = request.app.state.platform_settings_store
    return store
