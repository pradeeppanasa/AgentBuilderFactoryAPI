"""Audit event writer (CLAUDE.md Section 61.4).

This is agent-runtime's own copy, not an import of the Factory Runtime's
equivalent module (Sprint 3 Phase 2 asks for one in each service; F8/R10 —
zero shared code between the Builder Runtime and the Generated Agent
Runtime, separate deployables with independent lifecycles).

Written now (Phase 1) rather than Phase 2 because Phase 1's own acceptance
criteria requires an `auth.tenant_mismatch` event to land in
panasa-audit-log; Phase 2 wires in the remaining event types
(agent.invoked, tool.invoked, tool.denied, auth.failed) using this same
function.
"""

from __future__ import annotations

import os
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import boto3
import structlog

logger = structlog.get_logger()

_AUDIT_TTL_DAYS = 90
_table: Any | None = None


def _get_table() -> Any:
    global _table
    if _table is None:
        region = os.environ.get("AWS_REGION", "eu-west-2")
        table_name = os.environ.get("DYNAMODB_AUDIT_LOG_TABLE", "panasa-audit-log")
        _table = boto3.resource("dynamodb", region_name=region).Table(table_name)
    return _table


def write_audit_event(
    *,
    tenant_id: str,
    event_type: str,
    agent_id: str,
    principal_id: str,
    action: str,
    resource: str,
    result: str,
    request_id: str = "",
    source_ip: str = "",
    extra: dict[str, Any] | None = None,
) -> None:
    """Never raises. Availability of the audit trail is best-effort — a
    DynamoDB hiccup here must never take down the /chat request it is
    describing. `extra` must never carry a secret value, LLM prompt/
    response content, or PII (R30/R61) — callers are responsible for that;
    this function does not scan for it.
    """
    now = datetime.now(UTC)
    item: dict[str, Any] = {
        "tenant_id": tenant_id,
        "event_id": str(uuid.uuid4()),
        "timestamp": now.isoformat(),
        "event_type": event_type,
        "agent_id": agent_id,
        "principal_id": principal_id,
        "action": action,
        "resource": resource,
        "result": result,
        "request_id": request_id,
        "source_ip": source_ip,
        "expires_at": int((now + timedelta(days=_AUDIT_TTL_DAYS)).timestamp()),
    }
    if extra:
        item.update(extra)
    try:
        _get_table().put_item(Item=item)
    except Exception as exc:  # noqa: BLE001 - best-effort, see docstring
        logger.warning("audit_event_write_failed", event_type=event_type, error=str(exc))
