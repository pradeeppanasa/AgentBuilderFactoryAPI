"""Guardrail policy library — DynamoDB CRUD (CLAUDE_Advanced_Config.md
Section 4.2/8). Tenant-scoped only, same as the KB library. Admin-only
write enforcement (Section 3.5: "created and managed by admins only") lives
at the API layer via require_role(), same pattern as every other
admin-gated route in this codebase (e.g. platform upgrade).
"""

from __future__ import annotations

import asyncio
import re
import uuid
from datetime import UTC, datetime
from typing import Any

from boto3.dynamodb.conditions import Key
from botocore.exceptions import ClientError

from app.config import Settings
from app.modules.guardrails.models import GuardrailPolicy
from app.shared.dynamodb_types import decimal_to_native, native_to_decimal


def _slugify(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return slug or "policy"


def _now() -> str:
    return datetime.now(UTC).isoformat()


class GuardrailPolicyNotFoundError(Exception):
    def __init__(self, policy_id: str) -> None:
        self.policy_id = policy_id
        super().__init__(f"Guardrail policy {policy_id!r} not found")


class GuardrailPolicyStore:
    def __init__(self, dynamodb_resource: Any, settings: Settings) -> None:
        self._dynamodb = dynamodb_resource
        self._settings = settings
        self._table = dynamodb_resource.Table(settings.dynamodb_guardrail_policies_table)

    async def ensure_table(self) -> None:
        def _create() -> None:
            try:
                table = self._dynamodb.create_table(
                    TableName=self._settings.dynamodb_guardrail_policies_table,
                    KeySchema=[
                        {"AttributeName": "tenant_id", "KeyType": "HASH"},
                        {"AttributeName": "policy_id", "KeyType": "RANGE"},
                    ],
                    AttributeDefinitions=[
                        {"AttributeName": "tenant_id", "AttributeType": "S"},
                        {"AttributeName": "policy_id", "AttributeType": "S"},
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
        **overrides: Any,
    ) -> GuardrailPolicy:
        now = _now()
        record = GuardrailPolicy(
            policy_id=f"{_slugify(name)}-{uuid.uuid4().hex[:6]}",
            tenant_id=tenant_id,
            name=name,
            description=description,
            created_by=created_by,
            created_at=now,
            updated_at=now,
            **overrides,
        )
        await asyncio.to_thread(
            self._table.put_item, Item=native_to_decimal(record.model_dump(mode="json"))
        )
        return record

    async def list_policies(self, tenant_id: str) -> list[GuardrailPolicy]:
        response = await asyncio.to_thread(
            self._table.query, KeyConditionExpression=Key("tenant_id").eq(tenant_id)
        )
        return [GuardrailPolicy(**decimal_to_native(item)) for item in response.get("Items", [])]

    async def get(self, tenant_id: str, policy_id: str) -> GuardrailPolicy | None:
        response = await asyncio.to_thread(
            self._table.get_item, Key={"tenant_id": tenant_id, "policy_id": policy_id}
        )
        item = response.get("Item")
        return GuardrailPolicy(**decimal_to_native(item)) if item else None

    async def update(self, tenant_id: str, policy_id: str, **updates: Any) -> GuardrailPolicy:
        record = await self.get(tenant_id, policy_id)
        if record is None:
            raise GuardrailPolicyNotFoundError(policy_id)
        updated = record.model_copy(update={**updates, "updated_at": _now()})
        await asyncio.to_thread(
            self._table.put_item, Item=native_to_decimal(updated.model_dump(mode="json"))
        )
        return updated

    async def delete(self, tenant_id: str, policy_id: str) -> None:
        await asyncio.to_thread(
            self._table.delete_item, Key={"tenant_id": tenant_id, "policy_id": policy_id}
        )
