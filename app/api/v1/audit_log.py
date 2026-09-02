"""Audit Log page (Priority 2 nav addition) — admin-only, read-only view
over the S3 WORM audit trail app.modules.audit.writer already produces
(config_change/deploy/rollback/block events, R15/Section 14)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel

from app.dependencies import get_audit_writer, get_tenant_id
from app.modules.audit.writer import AuditEvent, AuditEventType, AuditWriter
from app.modules.auth.dependencies import require_role
from app.modules.auth.schemas import CurrentUser

router = APIRouter(prefix="/admin/audit-log", tags=["audit-log"])


class AuditLogResponse(BaseModel):
    items: list[AuditEvent]


@router.get("", response_model=AuditLogResponse)
async def list_audit_log(
    tenant_id: Annotated[str, Depends(get_tenant_id)],
    _current_user: Annotated[CurrentUser, Depends(require_role())],
    audit_writer: Annotated[AuditWriter, Depends(get_audit_writer)],
    date_from: Annotated[str | None, Query()] = None,
    date_to: Annotated[str | None, Query()] = None,
    event_type: Annotated[AuditEventType | None, Query()] = None,
    actor: Annotated[str | None, Query()] = None,
    resource: Annotated[str | None, Query(description="agent_id to filter by")] = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
) -> AuditLogResponse:
    # Default window: last 7 days — matches the Runs summary's own default
    # (Section 10) so a first-load page doesn't try to scan the entire
    # audit history.
    today = datetime.now(UTC).date()
    resolved_from = date_from or (today - timedelta(days=7)).isoformat()
    resolved_to = date_to or today.isoformat()

    items = await audit_writer.list_events(
        tenant_id=tenant_id,
        date_from=resolved_from,
        date_to=resolved_to,
        event_type=event_type,
        actor=actor,
        agent_id=resource,
        limit=limit,
    )
    return AuditLogResponse(items=items)
