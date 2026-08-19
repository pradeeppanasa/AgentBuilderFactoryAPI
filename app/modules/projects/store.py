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
from app.modules.projects.models import ProjectRecord, ProjectStatus
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
        self,
        tenant_id: str,
        name: str,
        description: str,
        created_by: str,
        tags: list[str] | None = None,
        guardrail_policy_id: str | None = None,
    ) -> ProjectRecord:
        now = _now()
        record = ProjectRecord(
            tenant_id=tenant_id,
            project_id=f"{_slugify(name)}-{uuid.uuid4().hex[:6]}",
            name=name,
            description=description,
            owner_email=created_by,
            status="active",
            agent_ids=[],
            tags=tags or [],
            guardrail_policy_id=guardrail_policy_id,
            created_by=created_by,
            created_at=now,
            updated_at=now,
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

    async def _require(self, tenant_id: str, project_id: str) -> ProjectRecord:
        record = await self.get(tenant_id, project_id)
        if record is None:
            raise ProjectNotFoundError(project_id)
        return record

    async def update(
        self,
        tenant_id: str,
        project_id: str,
        updated_by: str,
        name: str | None = None,
        description: str | None = None,
        tags: list[str] | None = None,
        guardrail_policy_id: str | None = None,
        status: ProjectStatus | None = None,
    ) -> ProjectRecord:
        record = await self._require(tenant_id, project_id)
        updates: dict[str, Any] = {"updated_at": _now()}
        if name is not None:
            updates["name"] = name
        if description is not None:
            updates["description"] = description
        if tags is not None:
            updates["tags"] = tags
        if guardrail_policy_id is not None:
            updates["guardrail_policy_id"] = guardrail_policy_id
        if status is not None:
            updates["status"] = status
        updated = record.model_copy(update=updates)
        await asyncio.to_thread(self._table.put_item, Item=updated.model_dump(mode="json"))
        return updated

    async def add_agent_id(self, tenant_id: str, project_id: str, agent_id: str) -> ProjectRecord:
        record = await self._require(tenant_id, project_id)
        if agent_id in record.agent_ids:
            return record
        updated = record.model_copy(
            update={"agent_ids": [*record.agent_ids, agent_id], "updated_at": _now()}
        )
        await asyncio.to_thread(self._table.put_item, Item=updated.model_dump(mode="json"))
        return updated

    async def remove_agent_id(
        self, tenant_id: str, project_id: str, agent_id: str
    ) -> ProjectRecord:
        record = await self._require(tenant_id, project_id)
        if agent_id not in record.agent_ids:
            return record
        updated = record.model_copy(
            update={
                "agent_ids": [a for a in record.agent_ids if a != agent_id],
                "updated_at": _now(),
            }
        )
        await asyncio.to_thread(self._table.put_item, Item=updated.model_dump(mode="json"))
        return updated

    async def delete(self, tenant_id: str, project_id: str) -> None:
        await asyncio.to_thread(
            self._table.delete_item, Key={"tenant_id": tenant_id, "project_id": project_id}
        )
