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

import json
import os
from decimal import Decimal
from typing import Any, cast

import boto3
from audit import write_audit_event
from config_hash import compute_config_hash


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


class ConfigIntegrityError(RuntimeError):
    """Sprint 4 Phase 5 (S-13b, R68) — the config this container loaded
    doesn't hash to the value compute.tf.j2 pinned into AGENT_CONFIG_HASH
    at deploy time. Raised out of load_agent_config() at import time
    (main.py's module-level `agent_config = load_agent_config()`), so the
    container crashes before serving a single request — ECS marks the
    task FAILED, matching R68's "refuse to start" exactly."""

    def __init__(self, agent_id: str, expected: str, actual: str) -> None:
        self.agent_id = agent_id
        self.expected = expected
        self.actual = actual
        super().__init__(
            f"Config integrity check failed for agent_id={agent_id!r}: "
            f"AGENT_CONFIG_HASH={expected!r} but loaded config hashes to {actual!r}"
        )


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


def _verify_config_hash(configuration: dict[str, Any], *, tenant_id: str, agent_id: str) -> None:
    """Sprint 4 Phase 5 (S-13b, R68). AGENT_CONFIG_HASH is only present on
    task definitions rendered after this feature shipped (compute.tf.j2)
    — absent entirely means an agent deployed before it existed, or a
    local/dev run; skipped, not failed, for backward compatibility. Once
    present, ANY mismatch is fatal — there is no partial/warn-only mode,
    since a mismatch is exactly the tampered-or-stale-config scenario R68
    exists to catch."""
    expected_hash = os.environ.get("AGENT_CONFIG_HASH")
    if not expected_hash:
        return

    actual_hash = compute_config_hash(configuration)
    if actual_hash == expected_hash:
        return

    write_audit_event(
        tenant_id=tenant_id,
        event_type="agent.config_integrity_failed",
        agent_id=agent_id,
        principal_id="agent-runtime",
        action="startup_config_verification",
        resource=agent_id,
        result="denied",
        extra={"expected_hash": expected_hash, "actual_hash": actual_hash},
    )
    raise ConfigIntegrityError(agent_id, expected_hash, actual_hash)


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
    # Sprint 4 Phase 5 (S-13b) bug fix — a REAL Factory-Runtime-written item
    # stores `configuration` as a JSON STRING (app/modules/registry/
    # versioner.py's _to_item(), _JSON_FIELDS), never a native DynamoDB Map.
    # This branch was missing entirely: every genuinely deployed agent
    # would hit the isinstance check below, raise AgentVersionNotFoundError,
    # and crash-loop forever — confirmed against a real moto-backed table,
    # not just by reading the code. Every existing test fixture in this
    # file passed `configuration` as a plain dict directly (bypassing the
    # JSON-string round trip entirely), which is exactly why this was never
    # caught: F8's zero-shared-code split also means the two services'
    # test suites never exercised each other's actual wire format before
    # (test_hitl_tool_approval_interop.py, added in S-11, is the only
    # other place in this codebase that does).
    if isinstance(configuration, str):
        configuration = json.loads(configuration)
    if not isinstance(configuration, dict):
        raise AgentVersionNotFoundError(agent_id, version)

    # Sprint 4 Phase 5 (S-13b, R68) — hashed BEFORE merging in
    # agent_id/tenant_id/name/version below (none of those are part of
    # AgentConfiguration; including them would never match the Factory
    # Runtime's own compute_config_hash(AgentConfiguration)). Decimal-
    # normalised first so a real DynamoDB-Number-typed field (never
    # actually true for `configuration` today — versioner.py always
    # stores it as a JSON string — but true for this file's own test
    # fixtures) hashes the same way json.loads() already naturally
    # produces native int/float, matching what model_dump(mode="json")
    # produced on the Factory Runtime side.
    configuration = _decimal_to_native(configuration)
    _verify_config_hash(configuration, tenant_id=tenant_id, agent_id=agent_id)

    config: dict[str, Any] = {
        "agent_id": agent_id,
        "tenant_id": tenant_id,
        "name": agent_item.get("name", agent_id),
        "version": version,
        **configuration,
    }
    return _decimal_to_native(config)


def get_current_agent_record(dynamodb: Any | None = None) -> dict[str, Any]:
    """Re-fetch this container's own AgentRecord fresh from panasa-agents.

    Sprint 3 Phase 1 (CLAUDE.md Section 64.1, R67) — unlike
    load_agent_config() above (called once at startup and cached for the
    process lifetime), the auth layer calls this on every request so that
    credential rotation/revocation (Section 64.4, Phase 8) and the R67
    tenant-isolation check see the agent's current DB state without
    waiting for a redeploy. A bare GetItem on a table keyed by
    {tenant_id, agent_id} is cheap enough to do per request.

    Returns the raw agent_item (tenant_id, agent_id, api_key_secret_arn,
    previous_api_key_secret_arn, previous_key_expires_at, api_key_revoked,
    ...) — not merged with AgentConfiguration like load_agent_config()
    does, since auth has no business reading system_prompt/tools/etc.
    """
    agent_id = os.environ["AGENT_ID"]
    tenant_id = os.environ["TENANT_ID"]
    region = os.environ.get("AWS_REGION", "eu-west-2")
    agents_table_name = os.environ.get("DYNAMODB_AGENTS_TABLE", "panasa-agents")

    resource = dynamodb or boto3.resource("dynamodb", region_name=region)
    agents_table = resource.Table(agents_table_name)
    response = agents_table.get_item(Key={"tenant_id": tenant_id, "agent_id": agent_id})
    item = response.get("Item")
    if item is None:
        raise AgentNotFoundError(tenant_id, agent_id)
    return cast("dict[str, Any]", _decimal_to_native(item))
