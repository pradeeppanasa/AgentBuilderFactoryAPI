"""Shared bootstrap for the 9 Step Functions Lambda handlers (Phase 11).

Each handler is a thin wrapper around the existing async app.modules.*
code — none of it is reimplemented here. Stores are built once at import
time (module-level, so warm Lambda invocations reuse the same boto3
resources) rather than per-invocation, the same tradeoff app/main.py's
lifespan hook makes for the FastAPI process.

R09/F8: these handlers are part of the Panasa Agent Builder *Runtime*
(the orchestrator), never the Generated Agent Runtime — they only ever
touch the Agent Registry and deployment status, never a business payload.
"""

from __future__ import annotations

import asyncio
from collections.abc import Coroutine
from typing import Any, TypeVar

from app.config import settings
from app.modules.audit.writer import AuditWriter
from app.modules.deployment.status_store import DeploymentStatusStore
from app.modules.git_provider._util import agent_repo_identifier
from app.modules.git_provider.factory import create_git_provider
from app.modules.git_provider.secrets import fetch_git_token
from app.modules.iac_generator.generator import IaCGenerator
from app.modules.observability.metrics import MetricsEmitter
from app.modules.platform.upgrade_store import PlatformUpgradeStatusStore
from app.modules.registry.store import AgentRegistryStore
from app.shared.aws_clients import (
    create_cloudwatch_client,
    create_codecommit_client,
    create_ecr_client,
    create_ecs_client,
)
from app.shared.dynamodb import create_dynamodb_resource
from app.shared.s3 import create_s3_client

_dynamodb = create_dynamodb_resource(settings)
_s3_client = create_s3_client(settings)
registry_store = AgentRegistryStore(_dynamodb, settings)
deployment_status_store = DeploymentStatusStore(_dynamodb, settings)
iac_generator = IaCGenerator(_s3_client, settings)
git_provider = create_git_provider(
    settings, fetch_git_token(settings), create_codecommit_client(settings)
)
audit_writer = AuditWriter(_s3_client, settings)
metrics_emitter = MetricsEmitter(create_cloudwatch_client(settings), settings)

# Phase 15 — platform upgrade handlers only (validating.py et al. never
# touch ECS/ECR; these clients exist purely for platform_*.py below).
platform_upgrade_store = PlatformUpgradeStatusStore(_dynamodb, settings)
ecs_client = create_ecs_client(settings)
ecr_client = create_ecr_client(settings)

_T = TypeVar("_T")


def run(coro: Coroutine[Any, Any, _T]) -> _T:
    """Lambda's handler contract is synchronous; every app.modules.* method
    this package calls is async. asyncio.run() is fine here — each
    invocation gets its own fresh event loop, and Lambda invocations are
    never concurrent within the same execution environment."""
    return asyncio.run(coro)


class StageFailure(Exception):
    """Raised by a handler to fail its Step Functions Task — caught by that
    state's `Catch: States.ALL -> MarkFailed` in deployment_workflow.json."""


def require(event: dict[str, Any], *keys: str) -> tuple[str, ...]:
    missing = [k for k in keys if not event.get(k)]
    if missing:
        raise StageFailure(f"Missing required event field(s): {missing}")
    return tuple(event[k] for k in keys)


def require_agent_repo(agent_id: str) -> str:
    """Section 45.2 — the per-agent panasa-iac-{agent_id} repo identifier,
    same computation app/api/v1/agents.py's deploy trigger already used to
    create/push to this repo."""
    repo = agent_repo_identifier(settings.git_provider, settings.git_org, agent_id)
    if repo is None:
        raise StageFailure("GIT_ORG is not configured")
    return repo
