"""Project registry — DynamoDB CRUD (CLAUDE.md Section 38.2/38.7).
Tenant-scoped, same shape as KnowledgeBaseStore/GuardrailPolicyStore."""

from __future__ import annotations

import asyncio
import re
import uuid
from datetime import UTC, datetime
from typing import Any

from boto3.dynamodb.conditions import Key
from botocore.exceptions import ClientError

from app.config import Settings
from app.modules.projects.models import ProjectRecord
from app.shared.dynamodb_types import decimal_to_native


def _slugify(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return slug or "project"


def _now() -> str:
    return datetime.now(UTC).isoformat()


class ProjectNotFoundError(Exception):
    def __init__(self, project_id: str) -> None:
        self.project_id = project_id
        super().__init__(f"Project {project_id!r} not found")


class ProjectStore:
    def __init__(self, dynamodb_resource: Any, settings: Settings) -> None:
        self._dynamodb = dynamodb_resource
        self._settings = settings
        self._table = dynamodb_resource.Table(settings.dynamodb_projects_table)

    async def ensure_table(self) -> None:
        def _create() -> None:
            try:
                table = self._dynamodb.create_table(
                    TableName=self._settings.dynamodb_projects_table,
                    KeySchema=[
                        {"AttributeName": "tenant_id", "KeyType": "HASH"},
                        {"AttributeName": "project_id", "KeyType": "RANGE"},
                    ],
                    AttributeDefinitions=[
                        {"AttributeName": "tenant_id", "AttributeType": "S"},
                        {"AttributeName": "project_id", "AttributeType": "S"},
                    ],
                    BillingMode="PAY_PER_REQUEST",
                )
                table.wait_until_exists()
            except ClientError as exc:
                if exc.response["Error"]["Code"] != "ResourceInUseException":
                    raise

        await asyncio.to_thread(_create)

    async def create(
        self, tenant_id: str, name: str, description: str, created_by: str
    ) -> ProjectRecord:
        now = _now()
        record = ProjectRecord(
            tenant_id=tenant_id,
            project_id=f"{_slugify(name)}-{uuid.uuid4().hex[:6]}",
            name=name,
            description=description,
            created_by=created_by,
            created_at=now,
            updated_by=created_by,
            updated_at=now,
            tags={},
        )
        await asyncio.to_thread(self._table.put_item, Item=record.model_dump(mode="json"))
        return record

    async def list_projects(self, tenant_id: str) -> list[ProjectRecord]:
        response = await asyncio.to_thread(
            self._table.query, KeyConditionExpression=Key("tenant_id").eq(tenant_id)
        )
        return [ProjectRecord(**decimal_to_native(item)) for item in response.get("Items", [])]

    async def get(self, tenant_id: str, project_id: str) -> ProjectRecord | None:
        response = await asyncio.to_thread(
            self._table.get_item, Key={"tenant_id": tenant_id, "project_id": project_id}
        )
        item = response.get("Item")
        return ProjectRecord(**decimal_to_native(item)) if item else None

    async def update(
        self,
        tenant_id: str,
        project_id: str,
        updated_by: str,
        name: str | None = None,
        description: str | None = None,
    ) -> ProjectRecord:
        record = await self.get(tenant_id, project_id)
        if record is None:
            raise ProjectNotFoundError(project_id)
        updates: dict[str, Any] = {"updated_by": updated_by, "updated_at": _now()}
        if name is not None:
            updates["name"] = name
        if description is not None:
            updates["description"] = description
        updated = record.model_copy(update=updates)
        await asyncio.to_thread(self._table.put_item, Item=updated.model_dump(mode="json"))
        return updated

    async def delete(self, tenant_id: str, project_id: str) -> None:
        await asyncio.to_thread(
            self._table.delete_item, Key={"tenant_id": tenant_id, "project_id": project_id}
        )
