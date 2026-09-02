"""Prompt Library — DynamoDB CRUD. Same tenant-scoped flat-CRUD shape as
app.modules.skills.store, minus versioning."""

from __future__ import annotations

import asyncio
import re
import uuid
from datetime import UTC, datetime
from typing import Any

from boto3.dynamodb.conditions import Key
from botocore.exceptions import ClientError

from app.config import Settings
from app.modules.prompts.models import PromptRecord
from app.shared.dynamodb_types import decimal_to_native


def _slugify(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return slug or "prompt"


def _now() -> str:
    return datetime.now(UTC).isoformat()


class PromptNotFoundError(Exception):
    def __init__(self, prompt_id: str) -> None:
        self.prompt_id = prompt_id
        super().__init__(f"Prompt {prompt_id!r} not found")


class PromptStore:
    def __init__(self, dynamodb_resource: Any, settings: Settings) -> None:
        self._dynamodb = dynamodb_resource
        self._settings = settings
        self._table = dynamodb_resource.Table(settings.dynamodb_prompts_table)

    async def ensure_table(self) -> None:
        def _create() -> None:
            try:
                table = self._dynamodb.create_table(
                    TableName=self._settings.dynamodb_prompts_table,
                    KeySchema=[
                        {"AttributeName": "tenant_id", "KeyType": "HASH"},
                        {"AttributeName": "prompt_id", "KeyType": "RANGE"},
                    ],
                    AttributeDefinitions=[
                        {"AttributeName": "tenant_id", "AttributeType": "S"},
                        {"AttributeName": "prompt_id", "AttributeType": "S"},
                    ],
                    BillingMode="PAY_PER_REQUEST",
                )
                table.wait_until_exists()
            except ClientError as exc:
                if exc.response["Error"]["Code"] != "ResourceInUseException":
                    raise

        await asyncio.to_thread(_create)

    async def create(
        self,
        tenant_id: str,
        name: str,
        content: str,
        created_by: str,
        tags: list[str] | None = None,
    ) -> PromptRecord:
        now = _now()
        record = PromptRecord(
            prompt_id=f"{_slugify(name)}-{uuid.uuid4().hex[:6]}",
            tenant_id=tenant_id,
            name=name,
            content=content,
            tags=tags or [],
            created_by=created_by,
            created_at=now,
            updated_by=created_by,
            updated_at=now,
        )
        await asyncio.to_thread(self._table.put_item, Item=record.model_dump(mode="json"))
        return record

    async def list_prompts(self, tenant_id: str) -> list[PromptRecord]:
        response = await asyncio.to_thread(
            self._table.query, KeyConditionExpression=Key("tenant_id").eq(tenant_id)
        )
        return [PromptRecord(**decimal_to_native(item)) for item in response.get("Items", [])]

    async def get(self, tenant_id: str, prompt_id: str) -> PromptRecord | None:
        response = await asyncio.to_thread(
            self._table.get_item, Key={"tenant_id": tenant_id, "prompt_id": prompt_id}
        )
        item = response.get("Item")
        return PromptRecord(**decimal_to_native(item)) if item else None

    async def update(
        self,
        tenant_id: str,
        prompt_id: str,
        updated_by: str,
        name: str | None = None,
        content: str | None = None,
        tags: list[str] | None = None,
    ) -> PromptRecord:
        record = await self.get(tenant_id, prompt_id)
        if record is None:
            raise PromptNotFoundError(prompt_id)
        updated = record.model_copy(
            update={
                "name": name if name is not None else record.name,
                "content": content if content is not None else record.content,
                "tags": tags if tags is not None else record.tags,
                "updated_by": updated_by,
                "updated_at": _now(),
            }
        )
        await asyncio.to_thread(self._table.put_item, Item=updated.model_dump(mode="json"))
        return updated

    async def delete(self, tenant_id: str, prompt_id: str) -> None:
        await asyncio.to_thread(
            self._table.delete_item, Key={"tenant_id": tenant_id, "prompt_id": prompt_id}
        )
