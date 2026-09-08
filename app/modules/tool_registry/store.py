"""Tool Allowlist Registry store (Sprint 3 Phase 7 — CLAUDE.md Section
62.2, S-13a). Single hash key (tool_id) — this is a small, flat,
platform-wide catalog, not tenant-scoped (a tool being APPROVED is a
statement about the tool integration itself, reviewed once by a Panasa/
tenant admin; whether a given agent may actually call it is Phase 4's
tool_policies, which IS per-agent)."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any

from botocore.exceptions import ClientError

from app.config import Settings
from app.modules.tool_registry.models import ToolRegistryEntry
from app.shared.dynamodb_types import decimal_to_native

_SEED_ENTRIES: list[dict[str, Any]] = [
    {
        "tool_id": "jira_search",
        "display_name": "Jira — Search Issues",
        "risk_level": "LOW",
        "status": "APPROVED",
    },
    {
        "tool_id": "jira_create_issue",
        "display_name": "Jira — Create Issue",
        "risk_level": "MEDIUM",
        "status": "APPROVED",
    },
    {
        "tool_id": "kb_search",
        "display_name": "Knowledge Base Search",
        "risk_level": "LOW",
        "status": "APPROVED",
    },
    {
        "tool_id": "send_email",
        "display_name": "Send Email",
        "risk_level": "MEDIUM",
        "status": "APPROVED",
    },
]
"""CLAUDE.md Section 63's exact seed list. Seeded once, only on the run
that actually creates the table (see ensure_table below) — an operator
who later deprecates/edits one of these rows must not have it silently
reset back to APPROVED on every Runtime restart."""


class ToolRegistryStore:
    def __init__(self, dynamodb_resource: Any, settings: Settings) -> None:
        self._dynamodb = dynamodb_resource
        self._settings = settings
        self._table = dynamodb_resource.Table(settings.dynamodb_tool_registry_table)

    async def ensure_table(self) -> None:
        def _create() -> bool:
            try:
                table = self._dynamodb.create_table(
                    TableName=self._settings.dynamodb_tool_registry_table,
                    KeySchema=[{"AttributeName": "tool_id", "KeyType": "HASH"}],
                    AttributeDefinitions=[{"AttributeName": "tool_id", "AttributeType": "S"}],
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
            await self._seed_initial_entries()

    async def _seed_initial_entries(self) -> None:
        now = datetime.now(UTC).isoformat()
        for seed in _SEED_ENTRIES:
            entry = ToolRegistryEntry(
                **seed,
                allowed_scopes=[],
                lambda_arn=None,
                last_reviewed_at=now,
                reviewed_by="system-seed",
            )
            await asyncio.to_thread(self._table.put_item, Item=entry.model_dump())

    async def get(self, tool_id: str) -> ToolRegistryEntry | None:
        response = await asyncio.to_thread(self._table.get_item, Key={"tool_id": tool_id})
        item = response.get("Item")
        if item is None:
            return None
        return ToolRegistryEntry(**decimal_to_native(item))
