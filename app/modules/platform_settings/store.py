"""Platform observability settings — DynamoDB CRUD, one item per tenant
(CLAUDE.md Section 39/R45, R45-7/8)."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any

from botocore.exceptions import ClientError

from app.config import Settings
from app.modules.platform_settings.models import GLOBAL_SETTING_ID, PlatformSettingsRecord
from app.shared.dynamodb_types import decimal_to_native


def _now() -> str:
    return datetime.now(UTC).isoformat()


class PlatformSettingsStore:
    def __init__(self, dynamodb_resource: Any, settings: Settings) -> None:
        self._dynamodb = dynamodb_resource
        self._settings = settings
        self._table = dynamodb_resource.Table(settings.dynamodb_platform_settings_table)

    async def ensure_table(self) -> None:
        def _create() -> None:
            try:
                table = self._dynamodb.create_table(
                    TableName=self._settings.dynamodb_platform_settings_table,
                    KeySchema=[
                        {"AttributeName": "tenant_id", "KeyType": "HASH"},
                        {"AttributeName": "setting_id", "KeyType": "RANGE"},
                    ],
                    AttributeDefinitions=[
                        {"AttributeName": "tenant_id", "AttributeType": "S"},
                        {"AttributeName": "setting_id", "AttributeType": "S"},
                    ],
                    BillingMode="PAY_PER_REQUEST",
                )
                table.wait_until_exists()
            except ClientError as exc:
                if exc.response["Error"]["Code"] != "ResourceInUseException":
                    raise

        await asyncio.to_thread(_create)

    async def get_or_create(self, tenant_id: str, actor: str) -> PlatformSettingsRecord:
        response = await asyncio.to_thread(
            self._table.get_item,
            Key={"tenant_id": tenant_id, "setting_id": GLOBAL_SETTING_ID},
        )
        item = response.get("Item")
        if item:
            return PlatformSettingsRecord(**decimal_to_native(item))

        record = PlatformSettingsRecord(tenant_id=tenant_id, updated_by=actor, updated_at=_now())
        await asyncio.to_thread(self._table.put_item, Item=record.model_dump(mode="json"))
        return record

    async def save(self, record: PlatformSettingsRecord) -> PlatformSettingsRecord:
        await asyncio.to_thread(self._table.put_item, Item=record.model_dump(mode="json"))
        return record
