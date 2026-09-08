"""Security/compliance event log — panasa-audit-log (CLAUDE.md Section 61,
Sprint 3 Phase 2).

Distinct from app/modules/audit/writer.py's AuditWriter/AuditEvent (S3 WORM,
Section 14 — six business-audit categories: config_change/deploy/
guardrail_decision/tool_call/rollback/block). That system is unchanged by
this one. Sprint 3 introduces a separate, additional mechanism: a much
larger security-event taxonomy (agent lifecycle, credential rotation/
revocation, per-invocation auth/tool decisions — CLAUDE.md Section 61.2),
stored in DynamoDB rather than S3 so both this Factory Runtime and the
Generated Agent Runtime (services/agent-runtime/audit.py — a separate,
zero-shared-code copy per F8/R10) can write to the same table with a bare
put_item, no S3 key-naming scheme needed. The two systems intentionally
coexist; neither replaces the other.

expires_at is written on every item as the TTL attribute. Sprint 4 Phase 2
(S-05) additionally enables the table's real TimeToLive + point-in-time-
recovery settings via boto3 at ensure_table() time — this table is a
Factory-Runtime-managed platform table (created here, not per-agent
Terraform), so "enable PITR/TTL" means calling DynamoDB's own admin APIs at
startup, the same place every other property of this table is already
declared, rather than a Terraform file that wouldn't apply to it.

No verification tool/endpoint exists yet to walk a tenant's chain and
recompute event_hash for each event to detect tampering — out of this
phase's own scope (only event-writing was asked for). Whoever builds one:
run app.shared.dynamodb_types.decimal_to_native() over a read-back item
before recomputing its hash — DynamoDB returns every number as Decimal,
and Decimal(123) vs int(123) stringify differently under
json.dumps(default=str), so hashing the raw read-back item never matches
even with zero tampering (see tests/test_security_audit_log_hash_chain.py's
own hash-determinism test for a worked example of this exact step).

Sprint 4 Phase 2 also adds hash-chain tamper-evidence (prev_hash/
event_hash per event). Deliberately NOT strictly race-free under
concurrent writes for the SAME tenant — see write_event's own docstring
for why a two-step read-tail/write-event/advance-tail approach was chosen
over a fully atomic (and considerably more complex) alternative, and what
that tradeoff actually costs.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from botocore.exceptions import ClientError

from app.config import Settings
from app.shared.logging import get_logger

log = get_logger()

_AUDIT_TTL_DAYS = 90
_AGENT_TIMESTAMP_INDEX = "agent_id-timestamp-index"
_CHAIN_TAIL_EVENT_ID = "_CHAIN_TAIL_"
"""Sentinel event_id (per tenant_id) holding the running chain head —
never a real event_type/agent_id, so every existing "list/filter events"
consumer already excludes it for free (none of them match on a row with
no agent_id/event_type). Never expires (no expires_at) — unlike individual
events, the chain head must survive past any single event's 90-day TTL for
the chain to keep extending indefinitely."""


class SecurityAuditLogStore:
    def __init__(self, dynamodb_resource: Any, settings: Settings) -> None:
        self._dynamodb = dynamodb_resource
        self._settings = settings
        self._table = dynamodb_resource.Table(settings.dynamodb_audit_log_table)

    async def ensure_table(self) -> None:
        def _create() -> bool:
            try:
                table = self._dynamodb.create_table(
                    TableName=self._settings.dynamodb_audit_log_table,
                    KeySchema=[
                        {"AttributeName": "tenant_id", "KeyType": "HASH"},
                        {"AttributeName": "event_id", "KeyType": "RANGE"},
                    ],
                    AttributeDefinitions=[
                        {"AttributeName": "tenant_id", "AttributeType": "S"},
                        {"AttributeName": "event_id", "AttributeType": "S"},
                        {"AttributeName": "agent_id", "AttributeType": "S"},
                        {"AttributeName": "timestamp", "AttributeType": "S"},
                    ],
                    GlobalSecondaryIndexes=[
                        {
                            "IndexName": _AGENT_TIMESTAMP_INDEX,
                            "KeySchema": [
                                {"AttributeName": "agent_id", "KeyType": "HASH"},
                                {"AttributeName": "timestamp", "KeyType": "RANGE"},
                            ],
                            "Projection": {"ProjectionType": "ALL"},
                        }
                    ],
                    BillingMode="PAY_PER_REQUEST",
                )
                table.wait_until_exists()
                return True
            except ClientError as exc:
                if exc.response["Error"]["Code"] != "ResourceInUseException":
                    raise
                return False

        created = await asyncio.to_thread(_create)
        if created:
            await self._enable_ttl_and_pitr()

    async def _enable_ttl_and_pitr(self) -> None:
        """Best-effort, only on the run that actually creates the table
        (idempotent-creation calls would otherwise redundantly re-issue
        these on every restart). Neither failing here should block the
        table — or the Runtime — from starting; a missing PITR/TTL admin
        setting degrades operational safety, it doesn't break correctness
        of any single write/read this store does."""
        client = self._dynamodb.meta.client
        try:
            await asyncio.to_thread(
                client.update_time_to_live,
                TableName=self._settings.dynamodb_audit_log_table,
                TimeToLiveSpecification={"Enabled": True, "AttributeName": "expires_at"},
            )
        except Exception as exc:  # noqa: BLE001 - best-effort, see docstring
            log.warning("audit_log.enable_ttl_failed", error=str(exc))
        try:
            await asyncio.to_thread(
                client.update_continuous_backups,
                TableName=self._settings.dynamodb_audit_log_table,
                PointInTimeRecoverySpecification={"PointInTimeRecoveryEnabled": True},
            )
        except Exception as exc:  # noqa: BLE001 - best-effort, see docstring
            log.warning("audit_log.enable_pitr_failed", error=str(exc))

    async def _get_chain_tail_hash(self, tenant_id: str) -> str | None:
        response = await asyncio.to_thread(
            self._table.get_item, Key={"tenant_id": tenant_id, "event_id": _CHAIN_TAIL_EVENT_ID}
        )
        item = response.get("Item")
        return item.get("latest_hash") if item else None

    async def _advance_chain_tail(self, tenant_id: str, new_hash: str) -> None:
        await asyncio.to_thread(
            self._table.put_item,
            Item={
                "tenant_id": tenant_id,
                "event_id": _CHAIN_TAIL_EVENT_ID,
                "latest_hash": new_hash,
            },
        )

    async def write_event(
        self,
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
        """Never raises — an audit-log outage must never take down the
        request it's describing (same posture as app.modules.audit.writer.
        AuditWriter and services/agent-runtime/audit.py's identical
        function). `extra` must never carry a secret value, LLM prompt/
        response content, or PII (R30/R61) — the caller's responsibility;
        this method does not scan for it.

        Sprint 4 Phase 2 (S-05) hash chain: prev_hash is read, then this
        event (including that prev_hash) is hashed and written, then the
        per-tenant chain tail is advanced to this event's own hash — three
        separate DynamoDB calls, not one atomic operation. Two events for
        the SAME tenant written concurrently could both read the same
        prev_hash and fork the chain, rather than being strictly
        serialized. A fully race-free design (e.g. a conditional-write
        retry loop, or claiming a monotonic sequence number atomically)
        was deliberately not built: every other audit write in this
        codebase is explicitly best-effort/fire-and-forget and must never
        block or retry against the request it's describing, and a rare
        fork under genuine concurrent writes for one tenant still leaves
        every individual event's own event_hash independently verifiable
        (tampering with an event's stored content changes its hash) — the
        only thing a fork weakens is strict total ordering across those
        specific concurrent events, not tamper-evidence of their content.
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
            prev_hash = await self._get_chain_tail_hash(tenant_id)
            item["prev_hash"] = prev_hash
            event_hash = hashlib.sha256(
                json.dumps(item, sort_keys=True, default=str).encode()
            ).hexdigest()
            item["event_hash"] = event_hash
            await asyncio.to_thread(self._table.put_item, Item=item)
            await self._advance_chain_tail(tenant_id, event_hash)
        except Exception as exc:  # noqa: BLE001 - best-effort, see docstring
            log.warning("audit_log.write_failed", event_type=event_type, error=str(exc))
