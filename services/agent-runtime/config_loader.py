"""Loads this container's own agent configuration from DynamoDB at startup.

Generic Agent Runtime (2026-09-03) — one Docker image, reused by every
deployed agent. AGENT_ID/TENANT_ID (compute.tf's only agent-specific env
vars) are the sole lookup keys; everything else this agent needs — model,
prompt, KB, tools, memory, guardrails, HITL — comes from here. Never
hardcode agent behaviour in Terraform or bake it into the image.

Reads the exact same two DynamoDB tables and key schema the Factory
Runtime's own registry uses (panasa-agents: {tenant_id, agent_id};
panasa-agent-versions: {agent_id, version} -> configuration), but this
service has ZERO dependency on the Factory Runtime itself (F8/R10): no
import of the Factory Runtime's Python package, no call to its API — just
direct, read-only access to tables its own ECS task role is scoped to
(authentication.tf.j2's ReadOwnConfig statement).

Deliberately plain dicts, not a shared Pydantic model — this service is
meant to keep working even if the Factory Runtime's own AgentConfiguration
schema evolves without a lockstep redeploy of every already-running agent
(a genuinely separate, independently-versioned service, not shared code).
"""

from __future__ import annotations

import os
from decimal import Decimal
from typing import Any

import boto3


class AgentNotFoundError(RuntimeError):
    def __init__(self, tenant_id: str, agent_id: str) -> None:
        self.tenant_id = tenant_id
        self.agent_id = agent_id
        super().__init__(f"Agent record not found: tenant_id={tenant_id} agent_id={agent_id}")


class AgentVersionNotFoundError(RuntimeError):
    def __init__(self, agent_id: str, version: int | None) -> None:
        self.agent_id = agent_id
        self.version = version
        super().__init__(f"Agent version not found: agent_id={agent_id} version={version}")


def _decimal_to_native(value: Any) -> Any:
    """DynamoDB returns numbers as Decimal — plain int/float is what every
    caller downstream (llm_client's temperature/max_tokens, JSON responses
    on /config) actually expects."""
    if isinstance(value, Decimal):
        return int(value) if value % 1 == 0 else float(value)
    if isinstance(value, dict):
        return {k: _decimal_to_native(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_decimal_to_native(v) for v in value]
    return value


def load_agent_config(dynamodb: Any | None = None) -> dict[str, Any]:
    """Load this container's own agent's full configuration.

    Called once at startup (main.py) — a config change takes effect on the
    NEXT deployed task, not by polling; that matches this platform's
    "new version = new deploy" model everywhere else (R08/R23).
    """
    agent_id = os.environ["AGENT_ID"]
    tenant_id = os.environ["TENANT_ID"]
    region = os.environ.get("AWS_REGION", "eu-west-2")
    agents_table_name = os.environ.get("DYNAMODB_AGENTS_TABLE", "panasa-agents")
    versions_table_name = os.environ.get("DYNAMODB_VERSIONS_TABLE", "panasa-agent-versions")

    resource = dynamodb or boto3.resource("dynamodb", region_name=region)

    agents_table = resource.Table(agents_table_name)
    agent_response = agents_table.get_item(Key={"tenant_id": tenant_id, "agent_id": agent_id})
    agent_item = agent_response.get("Item")
    if agent_item is None:
        raise AgentNotFoundError(tenant_id, agent_id)

    # live_version is None until THIS exact deploy's own HEALTH_CHECK stage
    # marks it live (F2/F12) — this container's own /health endpoint is
    # that check, so on an agent's very first deploy current_version (the
    # version just applied, not yet officially "live") is the only value
    # available yet. Falling back to it here is what lets the very first
    # health check succeed at all.
    raw_version = agent_item.get("live_version") or agent_item.get("current_version")
    if raw_version is None:
        raise AgentVersionNotFoundError(agent_id, None)
    version = int(raw_version)

    versions_table = resource.Table(versions_table_name)
    version_response = versions_table.get_item(Key={"agent_id": agent_id, "version": version})
    version_item = version_response.get("Item")
    if version_item is None:
        raise AgentVersionNotFoundError(agent_id, version)

    configuration = version_item.get("configuration")
    if not isinstance(configuration, dict):
        raise AgentVersionNotFoundError(agent_id, version)

    config: dict[str, Any] = {
        "agent_id": agent_id,
        "tenant_id": tenant_id,
        "name": agent_item.get("name", agent_id),
        "version": version,
        **configuration,
    }
    return _decimal_to_native(config)
