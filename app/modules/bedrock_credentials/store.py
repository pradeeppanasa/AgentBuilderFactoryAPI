"""Bedrock credential library — DynamoDB CRUD. Tenant-scoped, same shape
as the Guardrail Policy / Knowledge Base libraries."""

from __future__ import annotations

import asyncio
import re
import uuid
from datetime import UTC, datetime
from typing import Any

from boto3.dynamodb.conditions import Key
from botocore.exceptions import ClientError

from app.config import Settings
from app.modules.bedrock_credentials.models import BedrockCredentialRecord
from app.shared.dynamodb_types import decimal_to_native


def _slugify(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return slug or "credential"


def _now() -> str:
    return datetime.now(UTC).isoformat()


class BedrockCredentialNotFoundError(Exception):
    def __init__(self, credential_id: str) -> None:
        self.credential_id = credential_id
        super().__init__(f"Bedrock credential {credential_id!r} not found")


class BedrockCredentialStore:
    def __init__(self, dynamodb_resource: Any, settings: Settings) -> None:
        self._dynamodb = dynamodb_resource
        self._settings = settings
        self._table = dynamodb_resource.Table(settings.dynamodb_bedrock_credentials_table)

    async def ensure_table(self) -> None:
        def _create() -> None:
            try:
                table = self._dynamodb.create_table(
                    TableName=self._settings.dynamodb_bedrock_credentials_table,
                    KeySchema=[
                        {"AttributeName": "tenant_id", "KeyType": "HASH"},
                        {"AttributeName": "credential_id", "KeyType": "RANGE"},
                    ],
                    AttributeDefinitions=[
                        {"AttributeName": "tenant_id", "AttributeType": "S"},
                        {"AttributeName": "credential_id", "AttributeType": "S"},
                    ],
                    BillingMode="PAY_PER_REQUEST",
                )
                table.wait_until_exists()
            except ClientError as exc:
                if exc.response["Error"]["Code"] != "ResourceInUseException":
                    raise

        await asyncio.to_thread(_create)

    async def create(
        self, tenant_id: str, name: str, role_arn: str, created_by: str
    ) -> BedrockCredentialRecord:
        now = _now()
        record = BedrockCredentialRecord(
            credential_id=f"{_slugify(name)}-{uuid.uuid4().hex[:6]}",
            tenant_id=tenant_id,
            name=name,
            role_arn=role_arn,
            created_by=created_by,
            created_at=now,
            updated_at=now,
        )
        await asyncio.to_thread(self._table.put_item, Item=record.model_dump(mode="json"))
        return record

    async def list_credentials(self, tenant_id: str) -> list[BedrockCredentialRecord]:
        response = await asyncio.to_thread(
            self._table.query, KeyConditionExpression=Key("tenant_id").eq(tenant_id)
        )
        return [
            BedrockCredentialRecord(**decimal_to_native(item))
            for item in response.get("Items", [])
        ]

    async def get(self, tenant_id: str, credential_id: str) -> BedrockCredentialRecord | None:
        response = await asyncio.to_thread(
            self._table.get_item, Key={"tenant_id": tenant_id, "credential_id": credential_id}
        )
        item = response.get("Item")
        return BedrockCredentialRecord(**decimal_to_native(item)) if item else None

    async def delete(self, tenant_id: str, credential_id: str) -> None:
        await asyncio.to_thread(
            self._table.delete_item, Key={"tenant_id": tenant_id, "credential_id": credential_id}
        )
