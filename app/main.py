import asyncio
import logging
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import litellm
from aws_xray_sdk.core import patch, xray_recorder
from aws_xray_sdk.core.async_context import AsyncContext
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.v1 import router as api_v1_router
from app.config import settings
from app.license.validator import LicenseError, LicenseValidator
from app.middleware.xray import XRayMiddleware
from app.modules.audit.writer import AuditWriter
from app.modules.auth.db import create_db_engine, create_session_factory
from app.modules.auth.secrets import fetch_jwt_secret
from app.modules.connectors.catalog import ConnectorCatalogStore
from app.modules.connectors.tester import ConnectorTester
from app.modules.deployment.orchestrator import DeploymentOrchestrator
from app.modules.deployment.status_store import DeploymentStatusStore
from app.modules.git_provider.factory import create_git_provider
from app.modules.git_provider.secrets import fetch_git_token
from app.modules.iac_generator.generator import IaCGenerator
from app.modules.observability.metrics import MetricsEmitter
from app.modules.platform.upgrade_orchestrator import PlatformUpgradeOrchestrator
from app.modules.platform.upgrade_store import PlatformUpgradeStatusStore
from app.modules.platform.version_service import PlatformVersionService
from app.modules.registry.store import AgentRegistryStore
from app.modules.secrets.manager import SecretsManager
from app.shared.aws_clients import (
    create_cloudwatch_client,
    create_codecommit_client,
    create_ecr_client,
    create_eventbridge_client,
    create_stepfunctions_client,
)
from app.shared.dynamodb import create_dynamodb_resource
from app.shared.logging import configure_logging, get_logger
from app.shared.redis_client import create_redis_client
from app.shared.s3 import create_s3_client
from app.shared.secrets_manager import create_secrets_manager_client

configure_logging(settings.log_level)
log = get_logger()

# X-Ray tracing (Section 14 Phase 14). AsyncContext is required for correct
# segment propagation across `await` boundaries — the SDK's default
# thread-local context does not work for an async app serving concurrent
# requests. patch(["boto3"]) instruments every DynamoDB/S3/etc. call as a
# subsegment automatically; no per-call-site code needed anywhere else.
#
# Both calls are guarded: configure() resolves sampling rules (a network call
# to the X-Ray API unless a local daemon answers) and patch() rewrites boto3's
# call path at import time. With no daemon reachable — local dev, CI, any
# non-AWS environment — either can hang or raise, which would take the whole
# process down before FastAPI is even constructed. Tracing is observability,
# not a correctness dependency: on failure we log and run untraced.
XRAY_ENABLED = False
try:
    xray_recorder.configure(
        service="panasa-agent-builder-runtime",
        context=AsyncContext(),
        context_missing="LOG_ERROR",
    )
    patch(["boto3"])
    XRAY_ENABLED = True
except Exception as exc:  # noqa: BLE001 — never let tracing setup block startup
    log.warning("xray.setup_failed", error=str(exc), error_type=type(exc).__name__)

# Expected, benign noise from this: any boto3 call made outside a request
# (lifespan startup, background jobs, tests) has no open segment to attach
# a subsegment to — context_missing="LOG_ERROR" logs and continues rather
# than raising, which is exactly the desired graceful degradation, but the
# SDK logs it at ERROR level via plain stdlib logging (not this app's
# structlog JSON pipeline). Quieted so it doesn't drown out real logs;
# X-Ray tracing itself is unaffected — only its own diagnostic chatter is.
logging.getLogger("aws_xray_sdk").setLevel(logging.CRITICAL)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    try:
        LicenseValidator(settings).validate()
    except LicenseError as exc:
        log.critical("license.invalid", reason=str(exc))
        raise SystemExit(1) from exc

    # R28: belt-and-suspenders — disabled both as a runtime flag and via env.
    litellm.telemetry = False
    os.environ["LITELLM_TELEMETRY"] = "False"

    dynamodb = create_dynamodb_resource(settings)
    app.state.dynamodb = dynamodb
    registry_store = AgentRegistryStore(dynamodb, settings)
    await registry_store.ensure_tables()
    app.state.registry_store = registry_store

    deployment_status_store = DeploymentStatusStore(dynamodb, settings)
    await deployment_status_store.ensure_table()
    app.state.deployment_status_store = deployment_status_store

    connector_catalog_store = ConnectorCatalogStore(dynamodb, settings)
    await connector_catalog_store.ensure_table()
    await connector_catalog_store.seed_global_connectors()
    app.state.connector_catalog_store = connector_catalog_store
    app.state.secrets_manager = SecretsManager(create_secrets_manager_client(settings))
    app.state.connector_tester = ConnectorTester()

    app.state.jwt_secret = await asyncio.to_thread(fetch_jwt_secret, settings)

    db_engine = create_db_engine(settings)
    app.state.db_engine = db_engine
    app.state.db_session_factory = create_session_factory(db_engine)

    s3_client = create_s3_client(settings)
    app.state.s3_client = s3_client
    app.state.iac_generator = IaCGenerator(s3_client, settings)

    app.state.redis_client = create_redis_client(settings)

    git_token = await asyncio.to_thread(fetch_git_token, settings)
    codecommit_client = create_codecommit_client(settings)
    app.state.git_provider = create_git_provider(settings, git_token, codecommit_client)

    eventbridge_client = create_eventbridge_client(settings)
    app.state.deployment_orchestrator = DeploymentOrchestrator(eventbridge_client, settings)

    app.state.metrics_emitter = MetricsEmitter(create_cloudwatch_client(settings), settings)
    app.state.audit_writer = AuditWriter(s3_client, settings)

    platform_upgrade_store = PlatformUpgradeStatusStore(dynamodb, settings)
    await platform_upgrade_store.ensure_table()
    app.state.platform_upgrade_store = platform_upgrade_store
    app.state.platform_version_service = PlatformVersionService(
        create_ecr_client(settings), settings
    )
    app.state.platform_upgrade_orchestrator = PlatformUpgradeOrchestrator(
        create_stepfunctions_client(settings), settings
    )

    log.info("runtime.startup", mode=settings.deployment_mode, version=settings.platform_version)
    yield
    await app.state.redis_client.aclose()
    await db_engine.dispose()
    log.info("runtime.shutdown")


app = FastAPI(
    title="Panasa Agent Builder Runtime",
    version=settings.platform_version,
    lifespan=lifespan,
)

# Skipped when recorder setup failed above — begin_segment() on an
# unconfigured recorder would fault every request, so a booting-but-500ing
# server is not "starts regardless". Untraced requests are the fallback.
if XRAY_ENABLED:
    app.add_middleware(XRayMiddleware, service_name="panasa-agent-builder-runtime")
else:
    log.warning("xray.middleware_disabled")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(api_v1_router)
