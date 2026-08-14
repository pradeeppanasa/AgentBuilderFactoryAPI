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
from app.modules.connectors.catalog import ConnectorCatalogStore
from app.modules.connectors.tester import ConnectorTester
from app.modules.deployment.orchestrator import DeploymentOrchestrator
from app.modules.deployment.status_store import DeploymentStatusStore
from app.modules.git_provider.base import GitProvider
from app.modules.iac_generator.generator import IaCGenerator
from app.modules.iac_generator.validator import IaCValidator
from app.modules.observability.metrics import MetricsEmitter
from app.modules.platform.upgrade_orchestrator import PlatformUpgradeOrchestrator
from app.modules.platform.upgrade_store import PlatformUpgradeStatusStore
from app.modules.platform.version_service import PlatformVersionService
from app.modules.registry.store import AgentRegistryStore
from app.modules.secrets.manager import SecretsManager
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
