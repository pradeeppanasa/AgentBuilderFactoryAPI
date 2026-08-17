"""HITL review queue — DynamoDB CRUD (CLAUDE.md Section 38.7/38.8)."""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime
from typing import Any

from boto3.dynamodb.conditions import Key
from botocore.exceptions import ClientError

from app.config import Settings
from app.modules.hitl.models import HitlReviewRecord, HitlReviewStatus
from app.shared.dynamodb_types import decimal_to_native


def _now() -> str:
    return datetime.now(UTC).isoformat()


class HitlReviewNotFoundError(Exception):
    def __init__(self, review_id: str) -> None:
        self.review_id = review_id
        super().__init__(f"HITL review {review_id!r} not found")


class HitlReviewAlreadyDecidedError(Exception):
    def __init__(self, review_id: str, status: HitlReviewStatus) -> None:
        self.review_id = review_id
        self.status = status
        super().__init__(f"HITL review {review_id!r} is already {status.value!r}")


class HitlReviewStore:
    def __init__(self, dynamodb_resource: Any, settings: Settings) -> None:
        self._dynamodb = dynamodb_resource
        self._settings = settings
        self._table = dynamodb_resource.Table(settings.dynamodb_hitl_reviews_table)

    async def ensure_table(self) -> None:
        def _create() -> None:
            try:
                table = self._dynamodb.create_table(
                    TableName=self._settings.dynamodb_hitl_reviews_table,
                    KeySchema=[
                        {"AttributeName": "tenant_id", "KeyType": "HASH"},
                        {"AttributeName": "review_id", "KeyType": "RANGE"},
                    ],
                    AttributeDefinitions=[
                        {"AttributeName": "tenant_id", "AttributeType": "S"},
                        {"AttributeName": "review_id", "AttributeType": "S"},
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
        agent_id: str,
        project_id: str | None,
        trigger_condition: str,
        context_summary: str,
        timeout_hours: int,
        requested_by: str,
    ) -> HitlReviewRecord:
        record = HitlReviewRecord(
            tenant_id=tenant_id,
            review_id=f"REV-{uuid.uuid4().hex[:10]}",
            agent_id=agent_id,
            project_id=project_id,
            trigger_condition=trigger_condition,
            context_summary=context_summary,
            status=HitlReviewStatus.PENDING,
            timeout_hours=timeout_hours,
            requested_by=requested_by,
            requested_at=_now(),
        )
        await asyncio.to_thread(self._table.put_item, Item=record.model_dump(mode="json"))
        return record

    async def list_reviews(
        self, tenant_id: str, status: HitlReviewStatus | None = None
    ) -> list[HitlReviewRecord]:
        response = await asyncio.to_thread(
            self._table.query, KeyConditionExpression=Key("tenant_id").eq(tenant_id)
        )
        records = [
            HitlReviewRecord(**decimal_to_native(item)) for item in response.get("Items", [])
        ]
        if status is not None:
            records = [r for r in records if r.status == status]
        return records

    async def get(self, tenant_id: str, review_id: str) -> HitlReviewRecord | None:
        response = await asyncio.to_thread(
            self._table.get_item, Key={"tenant_id": tenant_id, "review_id": review_id}
        )
        item = response.get("Item")
        return HitlReviewRecord(**decimal_to_native(item)) if item else None

    async def _require_pending(self, tenant_id: str, review_id: str) -> HitlReviewRecord:
        record = await self.get(tenant_id, review_id)
        if record is None:
            raise HitlReviewNotFoundError(review_id)
        if record.status != HitlReviewStatus.PENDING:
            raise HitlReviewAlreadyDecidedError(review_id, record.status)
        return record

    async def _decide(
        self,
        tenant_id: str,
        review_id: str,
        new_status: HitlReviewStatus,
        reviewed_by: str,
        decision_reason: str | None,
    ) -> HitlReviewRecord:
        record = await self._require_pending(tenant_id, review_id)
        updated = record.model_copy(
            update={
                "status": new_status,
                "reviewed_by": reviewed_by,
                "reviewed_at": _now(),
                "decision_reason": decision_reason,
            }
        )
        await asyncio.to_thread(self._table.put_item, Item=updated.model_dump(mode="json"))
        return updated

    async def approve(
        self, tenant_id: str, review_id: str, reviewed_by: str, decision_reason: str | None
    ) -> HitlReviewRecord:
        return await self._decide(
            tenant_id, review_id, HitlReviewStatus.APPROVED, reviewed_by, decision_reason
        )

    async def reject(
        self, tenant_id: str, review_id: str, reviewed_by: str, decision_reason: str | None
    ) -> HitlReviewRecord:
        return await self._decide(
            tenant_id, review_id, HitlReviewStatus.REJECTED, reviewed_by, decision_reason
        )

    async def request_info(
        self, tenant_id: str, review_id: str, reviewed_by: str, decision_reason: str | None
    ) -> HitlReviewRecord:
        return await self._decide(
            tenant_id, review_id, HitlReviewStatus.INFO_REQUESTED, reviewed_by, decision_reason
        )
