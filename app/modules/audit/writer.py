"""Audit event writer — S3 WORM (CLAUDE.md Section 14 Phase 14).

Six event types per spec: config_change, deploy, guardrail_decision,
tool_call, rollback, block. guardrail_decision and tool_call happen at
agent *invocation* time, inside the Generated Agent Runtime (F8) — a
separate service this repo doesn't build — so only the four event types
this Builder Runtime actually produces (config_change, deploy, rollback,
block) have real call sites here (app/api/v1/agents.py,
app/modules/security/policy_enforcement.py). The other two are defined in
AuditEventType for schema consistency with whatever eventually writes them.

Write failures are logged, never raised — the same fail-open posture as
app.modules.observability.metrics for the same reason: an audit-bucket
outage blocking agent creation/deployment outright would be a worse
failure mode than a gap in the audit trail. Unlike Redis rate limiting
(R39) this isn't a security *control* being bypassed, just a compliance
side-channel that can be reconciled after the fact from CloudTrail/app logs.

R15: `summary`/`metadata` must stay human-readable and non-sensitive —
never raw prompts, tool payloads, or secret values (same rule Section 4.4's
StageResult.output_summary and Section 12's telemetry sanitiser both
already apply elsewhere in this codebase).
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any, Literal

from pydantic import BaseModel, Field

from app.config import Settings
from app.shared.logging import get_logger

log = get_logger()

AuditEventType = Literal[
    "config_change", "deploy", "guardrail_decision", "tool_call", "rollback", "block"
]


class AuditEvent(BaseModel):
    event_type: AuditEventType
    tenant_id: str
    agent_id: str | None = None
    actor: str
    summary: str
    metadata: dict[str, Any] = Field(default_factory=dict)
    occurred_at: str  # ISO 8601


class AuditWriter:
    def __init__(self, s3_client: Any, settings: Settings) -> None:
        self._s3 = s3_client
        self._settings = settings

    async def write(self, event: AuditEvent) -> str | None:
        """Returns the S3 key written, or None if the write failed
        (logged) or AUDIT_S3_BUCKET isn't configured (also logged —
        audit_enabled agents shouldn't silently produce no audit trail)."""
        if not self._settings.audit_s3_bucket:
            log.warning("audit.write.skipped", reason="AUDIT_S3_BUCKET not configured")
            return None

        date = event.occurred_at[:10]
        key = f"audit/{event.tenant_id}/{event.event_type}/{date}/{uuid.uuid4().hex}.json"

        try:
            await asyncio.to_thread(
                self._s3.put_object,
                Bucket=self._settings.audit_s3_bucket,
                Key=key,
                Body=event.model_dump_json().encode("utf-8"),
                ContentType="application/json",
            )
        except Exception:
            log.warning("audit.write.failed", event_type=event.event_type, exc_info=True)
            return None

        log.info("audit.write.succeeded", event_type=event.event_type, s3_key=key)
        return key

    async def list_events(
        self,
        tenant_id: str,
        date_from: str,
        date_to: str,
        event_type: AuditEventType | None = None,
        actor: str | None = None,
        agent_id: str | None = None,
        limit: int = 100,
    ) -> list[AuditEvent]:
        """Audit Log page (Priority 2 nav addition) — S3 has no query
        engine, so this lists every object key under
        audit/{tenant_id}/[{event_type}/] (paginated via
        list_objects_v2's own continuation token, capped at 500 keys
        scanned per call so a huge date range can't run away), keeps only
        the ones whose {date} key segment falls in [date_from, date_to],
        fetches THOSE bodies, and filters by actor/agent_id after
        parsing. Bounded, not indexed — fine for this stage's audit
        volume (same "not built for scale yet" caveat every other
        S3-backed list in this Runtime carries), never a reason to skip
        audit logging itself (R15/Section 14)."""
        if not self._settings.audit_s3_bucket:
            return []

        prefix = f"audit/{tenant_id}/{event_type}/" if event_type else f"audit/{tenant_id}/"
        matching_keys: list[str] = []
        continuation_token: str | None = None
        scanned = 0
        while scanned < 500:
            kwargs: dict[str, Any] = {"Bucket": self._settings.audit_s3_bucket, "Prefix": prefix}
            if continuation_token:
                kwargs["ContinuationToken"] = continuation_token
            try:
                response = await asyncio.to_thread(self._s3.list_objects_v2, **kwargs)
            except Exception:
                log.warning("audit.list.failed", exc_info=True)
                return []

            for obj in response.get("Contents", []):
                key = obj["Key"]
                # Key shape: audit/{tenant_id}/{event_type}/{date}/{uuid}.json
                parts = key.split("/")
                if len(parts) < 4:
                    continue
                key_date = parts[3]
                if date_from <= key_date <= date_to:
                    matching_keys.append(key)
            scanned += len(response.get("Contents", []))

            if not response.get("IsTruncated"):
                break
            continuation_token = response.get("NextContinuationToken")

        events: list[AuditEvent] = []
        for key in matching_keys:
            if len(events) >= limit:
                break
            try:
                obj = await asyncio.to_thread(
                    self._s3.get_object, Bucket=self._settings.audit_s3_bucket, Key=key
                )
                body = await asyncio.to_thread(obj["Body"].read)
                event = AuditEvent.model_validate_json(body)
            except Exception:
                log.warning("audit.list.read_failed", s3_key=key, exc_info=True)
                continue
            if actor is not None and event.actor != actor:
                continue
            if agent_id is not None and event.agent_id != agent_id:
                continue
            events.append(event)

        events.sort(key=lambda e: e.occurred_at, reverse=True)
        return events
