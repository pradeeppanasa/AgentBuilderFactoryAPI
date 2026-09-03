"""Memory (CLAUDE.md Section 23) — "none" | "session" | "persistent".

session: in-process only, scoped by session_id, lost on task restart —
matches the spec's own "session: In-memory (ECS) | Cleared: Each new
session" description exactly; no external store needed since a new
session_id already starts clean.

persistent: panasa-memory (PK={agent_id}#{user_id}, SK=memory_id, native
DynamoDB TTL via expires_at — Section 4.8). Each turn appends a new memory
item rather than updating one — matches "Every new version is a new
DynamoDB record" elsewhere in this platform (R08), and keeps a save from
racing a concurrent read. Section 23's summarise_and_compress() (LLM
compression once memory count crosses a threshold) is NOT implemented here
— a deliberate scope cut, not an oversight: this keeps memory.py correct
and simple for a first working version; unbounded growth is bounded only
by the per-user memory count this loads (see _MAX_PERSISTENT_MEMORIES).
"""

from __future__ import annotations

import asyncio
import os
import time
import uuid
from typing import Any

import boto3
from boto3.dynamodb.conditions import Key

_MAX_SESSION_TURNS_DEFAULT = 50
_MAX_PERSISTENT_MEMORIES = 20


class MemoryManager:
    def __init__(
        self,
        agent_id: str,
        memory_type: str,
        ttl_days: int = 30,
        max_session_turns: int = _MAX_SESSION_TURNS_DEFAULT,
        dynamodb: Any | None = None,
    ) -> None:
        self._agent_id = agent_id
        self._memory_type = memory_type
        self._ttl_days = ttl_days
        self._max_session_turns = max_session_turns
        self._session_turns: dict[str, list[tuple[str, str]]] = {}
        region = os.environ.get("AWS_REGION", "eu-west-2")
        self._dynamodb = dynamodb or boto3.resource("dynamodb", region_name=region)
        self._table_name = os.environ.get("DYNAMODB_MEMORY_TABLE", "panasa-memory")

    async def load(self, session_id: str, user_id: str | None) -> str:
        if self._memory_type == "session":
            return self._load_session(session_id)
        if self._memory_type == "persistent" and user_id:
            return await self._load_persistent(user_id)
        return ""

    async def save(self, session_id: str, user_id: str | None, message: str, response: str) -> None:
        if self._memory_type == "session":
            self._save_session(session_id, message, response)
        elif self._memory_type == "persistent" and user_id:
            await self._save_persistent(user_id, session_id, message, response)

    def _load_session(self, session_id: str) -> str:
        turns = self._session_turns.get(session_id, [])
        return "\n".join(f"User: {m}\nAssistant: {r}" for m, r in turns)

    def _save_session(self, session_id: str, message: str, response: str) -> None:
        turns = self._session_turns.setdefault(session_id, [])
        turns.append((message, response))
        if len(turns) > self._max_session_turns:
            del turns[: -self._max_session_turns]

    async def _load_persistent(self, user_id: str) -> str:
        table = self._dynamodb.Table(self._table_name)
        response = await asyncio.to_thread(
            table.query,
            KeyConditionExpression=Key("pk").eq(f"{self._agent_id}#{user_id}"),
            Limit=_MAX_PERSISTENT_MEMORIES,
            ScanIndexForward=False,
        )
        items = response.get("Items", [])
        return "\n".join(f"- {item['content']}" for item in items if item.get("content"))

    async def _save_persistent(
        self, user_id: str, session_id: str, message: str, response: str
    ) -> None:
        table = self._dynamodb.Table(self._table_name)
        now = int(time.time())
        item = {
            "pk": f"{self._agent_id}#{user_id}",
            "memory_id": str(uuid.uuid4()),
            "agent_id": self._agent_id,
            "user_id": user_id,
            "memory_type": "conversation_summary",
            "content": f"User asked: {message}\nAssistant replied: {response}"[:2000],
            "source_session_id": session_id,
            "expires_at": now + (self._ttl_days * 86400),
            "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now)),
        }
        await asyncio.to_thread(table.put_item, Item=item)
