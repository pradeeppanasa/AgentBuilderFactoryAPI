"""Deployment status store — DynamoDB CRUD (Section 4.4, F2).

PK=agent_id, SK=deployment_id (direct lookup once agent_id is known — e.g.
GET /agents/{agent_id}/deployments). A GSI on deployment_id lets
GET /deployments/{deployment_id} (Section 5.3 — a top-level route with no
agent_id in the path) resolve a deployment without knowing its agent
up front.

R03/F2: the Runtime only writes the initial record when a deployment is
triggered. Every subsequent stage update comes from the customer-side
CI/CD writing directly to this table; the Runtime never mutates stage
results itself.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from typing import Any

from boto3.dynamodb.conditions import Key
from botocore.exceptions import ClientError

from app.config import Settings
from app.modules.deployment.models import (
    DeploymentRecord,
    DeploymentStatus,
    StageResult,
    StageStatus,
)
from app.shared.dynamodb_types import decimal_to_native

_DEPLOYMENT_ID_INDEX = "deployment-id-index"

_TERMINAL_STAGE_STATUSES = {"PASSED", "FAILED", "SKIPPED", "BLOCKED"}


def _now() -> str:
    return datetime.now(UTC).isoformat()


class DeploymentNotFoundError(Exception):
    def __init__(self, agent_id: str, deployment_id: str) -> None:
        self.agent_id = agent_id
        self.deployment_id = deployment_id
        super().__init__(f"Deployment {deployment_id!r} not found for agent {agent_id!r}")


class DeploymentStatusStore:
    def __init__(self, dynamodb_resource: Any, settings: Settings) -> None:
        self._dynamodb = dynamodb_resource
        self._settings = settings
        self._table = dynamodb_resource.Table(settings.dynamodb_deployments_table)

    async def ensure_table(self) -> None:
        def _create() -> None:
            try:
                table = self._dynamodb.create_table(
                    TableName=self._settings.dynamodb_deployments_table,
                    KeySchema=[
                        {"AttributeName": "agent_id", "KeyType": "HASH"},
                        {"AttributeName": "deployment_id", "KeyType": "RANGE"},
                    ],
                    AttributeDefinitions=[
                        {"AttributeName": "agent_id", "AttributeType": "S"},
                        {"AttributeName": "deployment_id", "AttributeType": "S"},
                    ],
                    GlobalSecondaryIndexes=[
                        {
                            "IndexName": _DEPLOYMENT_ID_INDEX,
                            "KeySchema": [{"AttributeName": "deployment_id", "KeyType": "HASH"}],
                            "Projection": {"ProjectionType": "ALL"},
                        }
                    ],
                    BillingMode="PAY_PER_REQUEST",
                )
                table.wait_until_exists()
            except ClientError as exc:
                if exc.response["Error"]["Code"] != "ResourceInUseException":
                    raise

        await asyncio.to_thread(_create)

    async def create_deployment(self, record: DeploymentRecord) -> None:
        await asyncio.to_thread(self._table.put_item, Item=self._to_item(record))

    async def update_stage(
        self,
        agent_id: str,
        deployment_id: str,
        stage: str,
        stage_status: StageStatus,
        output_summary: str | None = None,
        blocking_issue: str | None = None,
        overall_status: DeploymentStatus | None = None,
        failure_reason: str | None = None,
        failed_stage: str | None = None,
    ) -> DeploymentRecord:
        """Write one stage's result (Section 4.4 — "populated after pipeline
        runs"). Read-modify-write is safe here because pipeline stages run
        strictly sequentially (step_functions/deployment_workflow.json),
        never concurrently.
        """
        record = await self.get_deployment(agent_id, deployment_id)
        if record is None:
            raise DeploymentNotFoundError(agent_id, deployment_id)

        now = _now()
        existing = record.stages.get(stage, StageResult(stage=stage))
        updated_stage = existing.model_copy(
            update={
                "status": stage_status,
                "output_summary": (
                    output_summary if output_summary is not None else existing.output_summary
                ),
                "blocking_issue": (
                    blocking_issue if blocking_issue is not None else existing.blocking_issue
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

        updated_record = record.model_copy(update=updates)
        await asyncio.to_thread(self._table.put_item, Item=self._to_item(updated_record))
        return updated_record

    async def get_deployment(self, agent_id: str, deployment_id: str) -> DeploymentRecord | None:
        response = await asyncio.to_thread(
            self._table.get_item, Key={"agent_id": agent_id, "deployment_id": deployment_id}
        )
        item = response.get("Item")
        return self._from_item(item) if item else None

    async def get_deployment_by_id(self, deployment_id: str) -> DeploymentRecord | None:
        response = await asyncio.to_thread(
            self._table.query,
            IndexName=_DEPLOYMENT_ID_INDEX,
            KeyConditionExpression=Key("deployment_id").eq(deployment_id),
            Limit=1,
        )
        items = response.get("Items", [])
        return self._from_item(items[0]) if items else None

    async def list_deployments(self, agent_id: str) -> list[DeploymentRecord]:
        """All deployments for an agent, newest first by triggered_at.

        deployment_id is a random identifier, not a timestamp, so ordering
        is done in Python rather than relying on the sort key's natural
        order.
        """
        deployments: list[DeploymentRecord] = []
        exclusive_start_key: dict[str, Any] | None = None
        while True:
            query_kwargs: dict[str, Any] = {"KeyConditionExpression": Key("agent_id").eq(agent_id)}
            if exclusive_start_key is not None:
                query_kwargs["ExclusiveStartKey"] = exclusive_start_key
            response = await asyncio.to_thread(self._table.query, **query_kwargs)
            deployments.extend(self._from_item(item) for item in response.get("Items", []))
            exclusive_start_key = response.get("LastEvaluatedKey")
            if exclusive_start_key is None:
                break
        deployments.sort(key=lambda d: d.triggered_at, reverse=True)
        return deployments

    @staticmethod
    def _to_item(record: DeploymentRecord) -> dict[str, Any]:
        data = record.model_dump(mode="json")
        data["stages"] = json.dumps(data["stages"])
        return data

    @staticmethod
    def _from_item(item: dict[str, Any]) -> DeploymentRecord:
        data = decimal_to_native(dict(item))
        if data.get("stages"):
            data["stages"] = json.loads(data["stages"])
        return DeploymentRecord(**data)
