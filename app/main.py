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
from app.modules.bedrock_credentials.store import BedrockCredentialStore
from app.modules.connectors.catalog import ConnectorCatalogStore
from app.modules.connectors.tester import ConnectorTester
from app.modules.deployment.iac_scan_runner import IaCScanRunner
from app.modules.deployment.orchestrator import DeploymentOrchestrator
from app.modules.deployment.pipeline_simulator import DeploymentPipelineSimulator
from app.modules.deployment.status_store import DeploymentStatusStore
from app.modules.git_provider.factory import create_git_provider
from app.modules.git_provider.secrets import fetch_git_token
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
from app.modules.prompts.store import PromptStore
from app.modules.registry.store import AgentRegistryStore
from app.modules.runs.store import RunStore
from app.modules.secrets.manager import SecretsManager
from app.modules.skills.store import SkillStore
from app.modules.task_planner.session_store import BuildWithAISessionStore
from app.modules.telemetry.emitter import TelemetryConfig, TelemetryEmitter
from app.shared.aws_clients import (
    create_bedrock_agent_client,
    create_bedrock_client,
    create_bedrock_client_with_credentials,
    create_bedrock_runtime_client,
    create_cloudwatch_client,
    create_codecommit_client,
    create_ecr_client,
    create_eventbridge_client,
    create_stepfunctions_client,
    create_sts_client,
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
    app.state.iac_validator = IaCValidator(settings.terraform_binary_path)

    app.state.redis_client = create_redis_client(settings)

    git_token = await asyncio.to_thread(fetch_git_token, settings)
    codecommit_client = create_codecommit_client(settings)
    app.state.git_provider = create_git_provider(settings, git_token, codecommit_client)

    eventbridge_client = create_eventbridge_client(settings)
    app.state.deployment_orchestrator = DeploymentOrchestrator(eventbridge_client, settings)
    iac_scan_runner = IaCScanRunner(settings, app.state.iac_validator)
    app.state.deployment_pipeline_simulator = DeploymentPipelineSimulator(
        deployment_status_store,
        registry_store,
        app.state.git_provider,
        settings,
        iac_scan_runner=iac_scan_runner,
    )

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

    # Advanced Config (CLAUDE_Advanced_Config.md Section 3/4/5, Section 37)
    knowledge_base_store = KnowledgeBaseStore(dynamodb, settings)
    await knowledge_base_store.ensure_table()
    app.state.knowledge_base_store = knowledge_base_store

    guardrail_policy_store = GuardrailPolicyStore(dynamodb, settings)
    await guardrail_policy_store.ensure_table()
    app.state.guardrail_policy_store = guardrail_policy_store

    playground_session_store = PlaygroundSessionStore(dynamodb, settings)
    await playground_session_store.ensure_table()
    app.state.playground_session_store = playground_session_store

    build_with_ai_session_store = BuildWithAISessionStore(dynamodb, settings)
    await build_with_ai_session_store.ensure_table()
    app.state.build_with_ai_session_store = build_with_ai_session_store

    bedrock_credential_store = BedrockCredentialStore(dynamodb, settings)
    await bedrock_credential_store.ensure_table()
    app.state.bedrock_credential_store = bedrock_credential_store

    # Section 38 — Projects
    project_store = ProjectStore(dynamodb, settings)
    await project_store.ensure_table()
    app.state.project_store = project_store

    # Section 38.3 — Skills catalog
    skill_store = SkillStore(dynamodb, settings)
    await skill_store.ensure_table()
    app.state.skill_store = skill_store

    # Prompt Library (Priority 2 nav addition)
    prompt_store = PromptStore(dynamodb, settings)
    await prompt_store.ensure_table()
    app.state.prompt_store = prompt_store

    # Section 38.7/38.8 — HITL review queue
    hitl_review_store = HitlReviewStore(dynamodb, settings)
    await hitl_review_store.ensure_table()
    app.state.hitl_review_store = hitl_review_store

    # Observability — Runs Feature, Phase 1
    run_store = RunStore(dynamodb, settings)
    await run_store.ensure_table()
    app.state.run_store = run_store

    # Section 39/R45, R45-7/8 — Admin observability settings
    platform_settings_store = PlatformSettingsStore(dynamodb, settings)
    await platform_settings_store.ensure_table()
    app.state.platform_settings_store = platform_settings_store

    app.state.guardrail_engine = GuardrailEngine(
        create_bedrock_runtime_client(settings), mock_enabled=settings.mock_bedrock_guardrails
    )
    app.state.bedrock_guardrail_provisioner = BedrockGuardrailProvisioner(
        create_bedrock_client(settings),
        sts_client=create_sts_client(settings),
        credential_store=bedrock_credential_store,
        client_factory=lambda creds: create_bedrock_client_with_credentials(settings, creds),
        mock_enabled=settings.mock_bedrock_guardrails,
    )
    app.state.bedrock_kb_provisioner = BedrockKnowledgeBaseProvisioner(
        create_bedrock_agent_client(settings),
        kb_role_arn=settings.bedrock_kb_role_arn or "",
        opensearch_collection_arn=settings.opensearch_collection_arn or "",
        aws_region=settings.aws_region,
        kb_documents_bucket=settings.kb_documents_bucket or "",
        mock_enabled=settings.mock_bedrock_kb,
    )

    # R16: TELEMETRY_ENABLED defaults false; categories default all-on so
    # flipping the master switch alone re-enables full telemetry. PUT
    # /platform/telemetry-config mutates this same object in place — the
    # emitter must see the change without a re-wire, see emitter.py's
    # TelemetryConfig docstring.
    telemetry_config = TelemetryConfig(enabled=settings.telemetry_enabled)
    app.state.telemetry_config = telemetry_config
    app.state.telemetry_emitter = TelemetryEmitter(settings, telemetry_config)

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
    allow_origins=settings.cors_allowed_origins_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(api_v1_router)
