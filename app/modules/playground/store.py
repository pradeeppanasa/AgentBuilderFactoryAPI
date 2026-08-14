"""Playground session store — DynamoDB CRUD (CLAUDE_Advanced_Config.md
Section 5/8). Keyed by agent_id (HASH) / session_id (RANGE), matching the
`panasa-transcripts` shape (Section 4.10) rather than the tenant_id-keyed
KB/Guardrail Policy libraries — a playground session is always accessed in
the context of one specific agent, and R01 tenant scoping is still enforced
at the application layer (the caller already resolved agent_id under a
tenant-checked AgentRegistryStore.get_agent() before ever reaching here);
tenant_id is carried as a plain attribute on the record for audit purposes.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime
from typing import Any

from botocore.exceptions import ClientError

from app.config import Settings
from app.modules.playground.models import PlaygroundSessionRecord, PlaygroundTurn
from app.shared.dynamodb_types import decimal_to_native


def _now() -> str:
    return datetime.now(UTC).isoformat()


class PlaygroundSessionNotFoundError(Exception):
    def __init__(self, session_id: str) -> None:
        self.session_id = session_id
        super().__init__(f"Playground session {session_id!r} not found")


class PlaygroundSessionStore:
    def __init__(self, dynamodb_resource: Any, settings: Settings) -> None:
        self._dynamodb = dynamodb_resource
        self._settings = settings
        self._table = dynamodb_resource.Table(settings.dynamodb_playground_sessions_table)

    async def ensure_table(self) -> None:
        def _create() -> None:
            try:
                table = self._dynamodb.create_table(
                    TableName=self._settings.dynamodb_playground_sessions_table,
                    KeySchema=[
                        {"AttributeName": "agent_id", "KeyType": "HASH"},
                        {"AttributeName": "session_id", "KeyType": "RANGE"},
                    ],
                    AttributeDefinitions=[
                        {"AttributeName": "agent_id", "AttributeType": "S"},
                        {"AttributeName": "session_id", "AttributeType": "S"},
                    ],
                    BillingMode="PAY_PER_REQUEST",
                )
                table.wait_until_exists()
            except ClientError as exc:
                if exc.response["Error"]["Code"] != "ResourceInUseException":
                    raise

        await asyncio.to_thread(_create)

    async def get_or_create(
        self, tenant_id: str, agent_id: str, session_id: str | None
    ) -> PlaygroundSessionRecord:
        if session_id is not None:
            existing = await self.get(agent_id, session_id)
            if existing is not None:
                return existing
        now = _now()
        record = PlaygroundSessionRecord(
            session_id=session_id or f"pg-{uuid.uuid4().hex[:12]}",
            agent_id=agent_id,
            tenant_id=tenant_id,
            turns=[],
            created_at=now,
            updated_at=now,
        )
        await asyncio.to_thread(self._table.put_item, Item=record.model_dump(mode="json"))
        return record

    async def get(self, agent_id: str, session_id: str) -> PlaygroundSessionRecord | None:
        response = await asyncio.to_thread(
            self._table.get_item, Key={"agent_id": agent_id, "session_id": session_id}
        )
        item = response.get("Item")
        return PlaygroundSessionRecord(**decimal_to_native(item)) if item else None

    async def append_turns(
        self, agent_id: str, session_id: str, new_turns: list[PlaygroundTurn]
    ) -> PlaygroundSessionRecord:
        record = await self.get(agent_id, session_id)
        if record is None:
            raise PlaygroundSessionNotFoundError(session_id)
        updated = record.model_copy(
            update={"turns": [*record.turns, *new_turns], "updated_at": _now()}
        )
        await asyncio.to_thread(self._table.put_item, Item=updated.model_dump(mode="json"))
        return updated
