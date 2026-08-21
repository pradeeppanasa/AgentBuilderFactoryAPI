"""Build with AI session store — DynamoDB CRUD (CLAUDE.md Section 42).

Same shape/pattern as app.modules.playground.store.PlaygroundSessionStore:
tenant-scoped rather than agent-scoped (a session isn't about one existing
agent, it's about a proposed system of agents that don't exist yet), so
keyed by tenant_id (HASH) / session_id (RANGE).
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime
from typing import Any

from botocore.exceptions import ClientError

from app.config import Settings
from app.modules.task_planner.models import BuildWithAISessionRecord
from app.shared.dynamodb_types import decimal_to_native, native_to_decimal


def _now() -> str:
    return datetime.now(UTC).isoformat()


def new_session_id() -> str:
    return f"bwai-{uuid.uuid4().hex[:12]}"


class BuildWithAISessionNotFoundError(Exception):
    def __init__(self, session_id: str) -> None:
        self.session_id = session_id
        super().__init__(f"Build with AI session {session_id!r} not found")


class BuildWithAISessionStore:
    def __init__(self, dynamodb_resource: Any, settings: Settings) -> None:
        self._dynamodb = dynamodb_resource
        self._settings = settings
        self._table = dynamodb_resource.Table(settings.dynamodb_build_with_ai_sessions_table)

    async def ensure_table(self) -> None:
        def _create() -> None:
            try:
                table = self._dynamodb.create_table(
                    TableName=self._settings.dynamodb_build_with_ai_sessions_table,
                    KeySchema=[
                        {"AttributeName": "tenant_id", "KeyType": "HASH"},
                        {"AttributeName": "session_id", "KeyType": "RANGE"},
                    ],
                    AttributeDefinitions=[
                        {"AttributeName": "tenant_id", "AttributeType": "S"},
                        {"AttributeName": "session_id", "AttributeType": "S"},
                    ],
                    BillingMode="PAY_PER_REQUEST",
                )
                table.wait_until_exists()
            except ClientError as exc:
                if exc.response["Error"]["Code"] != "ResourceInUseException":
                    raise

        await asyncio.to_thread(_create)

    async def put(self, record: BuildWithAISessionRecord) -> None:
        item = native_to_decimal(record.model_dump(mode="json"))
        await asyncio.to_thread(self._table.put_item, Item=item)

    async def get(self, tenant_id: str, session_id: str) -> BuildWithAISessionRecord | None:
        response = await asyncio.to_thread(
            self._table.get_item, Key={"tenant_id": tenant_id, "session_id": session_id}
        )
        item = response.get("Item")
        return BuildWithAISessionRecord(**decimal_to_native(item)) if item else None

    async def require(self, tenant_id: str, session_id: str) -> BuildWithAISessionRecord:
        record = await self.get(tenant_id, session_id)
        if record is None:
            raise BuildWithAISessionNotFoundError(session_id)
        return record
