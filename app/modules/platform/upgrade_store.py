"""Platform upgrade status store — DynamoDB CRUD (Phase 15).

Same shape as app.modules.deployment.status_store.DeploymentStatusStore —
PK-only (upgrade_id), no tenant partition (see upgrade_models.py's
docstring for why). list_upgrades() uses a scan rather than a query since
there's no natural partition to query by and upgrades are rare, admin-only
operations — this table will never hold enough items for a scan to matter.

Unlike DeploymentStatusStore, `stages` here is stored as a native DynamoDB
Map, not a JSON string — that store's JSON-string encoding exists
specifically so codebuild/scripts/write_stage_result.sh (a bash script,
manipulating it with jq) can read-modify-write it; nothing but this Python
store ever touches panasa-platform-upgrades, so there's no bash consumer to
accommodate and boto3's own dict<->Map serialization is simpler.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any

from botocore.exceptions import ClientError

from app.config import Settings
from app.modules.platform.upgrade_models import (
    PlatformUpgradeRecord,
    UpgradeStageResult,
    UpgradeStageStatus,
    UpgradeStatus,
)
from app.shared.dynamodb_types import decimal_to_native

_TERMINAL_STAGE_STATUSES = {"PASSED", "FAILED"}


def _now() -> str:
    return datetime.now(UTC).isoformat()


class UpgradeNotFoundError(Exception):
    def __init__(self, upgrade_id: str) -> None:
        self.upgrade_id = upgrade_id
        super().__init__(f"Platform upgrade {upgrade_id!r} not found")


class PlatformUpgradeStatusStore:
    def __init__(self, dynamodb_resource: Any, settings: Settings) -> None:
        self._dynamodb = dynamodb_resource
        self._settings = settings
        self._table = dynamodb_resource.Table(settings.dynamodb_platform_upgrades_table)

    async def ensure_table(self) -> None:
        def _create() -> None:
            try:
                table = self._dynamodb.create_table(
                    TableName=self._settings.dynamodb_platform_upgrades_table,
                    KeySchema=[{"AttributeName": "upgrade_id", "KeyType": "HASH"}],
                    AttributeDefinitions=[{"AttributeName": "upgrade_id", "AttributeType": "S"}],
                    BillingMode="PAY_PER_REQUEST",
                )
                table.wait_until_exists()
            except ClientError as exc:
                if exc.response["Error"]["Code"] != "ResourceInUseException":
                    raise

        await asyncio.to_thread(_create)

    async def create_upgrade(self, record: PlatformUpgradeRecord) -> None:
        await asyncio.to_thread(self._table.put_item, Item=self._to_item(record))

    async def get_upgrade(self, upgrade_id: str) -> PlatformUpgradeRecord | None:
        response = await asyncio.to_thread(self._table.get_item, Key={"upgrade_id": upgrade_id})
        item = response.get("Item")
        return self._from_item(item) if item else None

    async def list_upgrades(self) -> list[PlatformUpgradeRecord]:
        upgrades: list[PlatformUpgradeRecord] = []
        exclusive_start_key: dict[str, Any] | None = None
        while True:
            scan_kwargs: dict[str, Any] = {}
            if exclusive_start_key is not None:
                scan_kwargs["ExclusiveStartKey"] = exclusive_start_key
            response = await asyncio.to_thread(self._table.scan, **scan_kwargs)
            upgrades.extend(self._from_item(item) for item in response.get("Items", []))
            exclusive_start_key = response.get("LastEvaluatedKey")
            if exclusive_start_key is None:
                break
        upgrades.sort(key=lambda u: u.triggered_at, reverse=True)
        return upgrades

    async def update_stage(
        self,
        upgrade_id: str,
        stage: str,
        stage_status: UpgradeStageStatus,
        output_summary: str | None = None,
        overall_status: UpgradeStatus | None = None,
        failure_reason: str | None = None,
        failed_stage: str | None = None,
        previous_task_definition_arn: str | None = None,
        new_task_definition_arn: str | None = None,
    ) -> PlatformUpgradeRecord:
        record = await self.get_upgrade(upgrade_id)
        if record is None:
            raise UpgradeNotFoundError(upgrade_id)

        now = _now()
        existing = record.stages.get(stage, UpgradeStageResult(stage=stage))
        updated_stage = existing.model_copy(
            update={
                "status": stage_status,
                "output_summary": (
                    output_summary if output_summary is not None else existing.output_summary
                ),
                "started_at": existing.started_at or (now if stage_status == "RUNNING" else None),
                "completed_at": (
                    now if stage_status in _TERMINAL_STAGE_STATUSES else existing.completed_at
                ),
            }
        )

        updates: dict[str, Any] = {
            "stages": {**record.stages, stage: updated_stage},
            "current_stage": stage,
            "updated_at": now,
        }
        if overall_status is not None:
            updates["status"] = overall_status
        if failure_reason is not None:
            updates["failure_reason"] = failure_reason
        if failed_stage is not None:
            updates["failed_stage"] = failed_stage
        if previous_task_definition_arn is not None:
            updates["previous_task_definition_arn"] = previous_task_definition_arn
        if new_task_definition_arn is not None:
            updates["new_task_definition_arn"] = new_task_definition_arn

        updated_record = record.model_copy(update=updates)
        await asyncio.to_thread(self._table.put_item, Item=self._to_item(updated_record))
        return updated_record

    @staticmethod
    def _to_item(record: PlatformUpgradeRecord) -> dict[str, Any]:
        return record.model_dump(mode="json")

    @staticmethod
    def _from_item(item: dict[str, Any]) -> PlatformUpgradeRecord:
        return PlatformUpgradeRecord(**decimal_to_native(dict(item)))
