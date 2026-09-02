"""Connector catalog — DynamoDB CRUD (Section 4.5 / Section 5.4 / Phase 12).

R01: every tenant-facing method takes tenant_id and scopes its DynamoDB
operations to it — except reads, which additionally fall back to the
GLOBAL partition (Section 4.5: "List connector catalog (global + tenant)").
Tenants can never write into the GLOBAL partition — create_connector()
always writes is_global=False under the caller's own tenant_id; only
seed_global_connectors() (called once at startup, like
AgentRegistryStore.ensure_tables()) ever writes GLOBAL_TENANT_ID.
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
from app.modules.connectors.models import GLOBAL_TENANT_ID, ConnectorRecord, ExecutorType
from app.shared.dynamodb_types import decimal_to_native


def _slugify(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return slug or "connector"


def _now() -> str:
    return datetime.now(UTC).isoformat()


# Section 5.1's own example agent references these three by name — seeded
# here so a fresh deployment has a non-empty catalog to build that example
# against. Purely illustrative; real global connectors are expected to be
# curated by Panasa over time, not hardcoded forever.
_GLOBAL_CONNECTOR_SEEDS: tuple[dict[str, Any], ...] = (
    {
        "connector_id": "jira",
        "name": "Jira",
        "executor_type": "http",
        "description": "Atlassian Jira issue tracking.",
        "input_schema": {"type": "object", "properties": {"issue_key": {"type": "string"}}},
        "output_schema": {"type": "object"},
        "endpoint_template": "https://{domain}.atlassian.net/rest/api/3",
        "credentials_required": ["api_key", "domain"],
    },
    {
        "connector_id": "salesforce",
        "name": "Salesforce",
        "executor_type": "http",
        "description": "Salesforce CRM records.",
        "input_schema": {"type": "object", "properties": {"object_id": {"type": "string"}}},
        "output_schema": {"type": "object"},
        "endpoint_template": "https://{domain}.salesforce.com/services/data/v60.0",
        "credentials_required": ["access_token", "domain"],
    },
    {
        "connector_id": "companies-house",
        "name": "Companies House Lookup",
        "executor_type": "http",
        "description": "UK Companies House company/officer lookup.",
        "input_schema": {"type": "object", "properties": {"company_number": {"type": "string"}}},
        "output_schema": {"type": "object"},
        "endpoint_template": "https://api.companieshouse.gov.uk",
        "credentials_required": ["api_key"],
    },
)


class ConnectorCatalogStore:
    def __init__(self, dynamodb_resource: Any, settings: Settings) -> None:
        self._dynamodb = dynamodb_resource
        self._settings = settings
        self._table = dynamodb_resource.Table(settings.dynamodb_connectors_table)

    async def ensure_table(self) -> None:
        def _create() -> None:
            try:
                table = self._dynamodb.create_table(
                    TableName=self._settings.dynamodb_connectors_table,
                    KeySchema=[
                        {"AttributeName": "tenant_id", "KeyType": "HASH"},
                        {"AttributeName": "connector_id", "KeyType": "RANGE"},
                    ],
                    AttributeDefinitions=[
                        {"AttributeName": "tenant_id", "AttributeType": "S"},
                        {"AttributeName": "connector_id", "AttributeType": "S"},
                    ],
                    BillingMode="PAY_PER_REQUEST",
                )
                table.wait_until_exists()
            except ClientError as exc:
                if exc.response["Error"]["Code"] != "ResourceInUseException":
                    raise

        await asyncio.to_thread(_create)

    async def seed_global_connectors(self) -> None:
        """Idempotent — always overwrites with the current seed definition,
        same as how the seeds themselves are just Panasa-curated constants,
        never user-edited."""
        now = _now()
        for seed in _GLOBAL_CONNECTOR_SEEDS:
            record = ConnectorRecord(
                tenant_id=GLOBAL_TENANT_ID,
                is_global=True,
                created_by="panasa",
                created_at=now,
                **seed,
            )
            await asyncio.to_thread(self._table.put_item, Item=record.model_dump(mode="json"))

    async def list_connectors(self, tenant_id: str) -> list[ConnectorRecord]:
        global_response, tenant_response = await asyncio.gather(
            asyncio.to_thread(
                self._table.query, KeyConditionExpression=Key("tenant_id").eq(GLOBAL_TENANT_ID)
            ),
            asyncio.to_thread(
                self._table.query, KeyConditionExpression=Key("tenant_id").eq(tenant_id)
            ),
        )
        records = [
            ConnectorRecord(**decimal_to_native(item))
            for item in global_response.get("Items", []) + tenant_response.get("Items", [])
        ]
        return records

    async def get_connector(self, tenant_id: str, connector_id: str) -> ConnectorRecord | None:
        response = await asyncio.to_thread(
            self._table.get_item, Key={"tenant_id": tenant_id, "connector_id": connector_id}
        )
        item = response.get("Item")
        if item is None:
            response = await asyncio.to_thread(
                self._table.get_item,
                Key={"tenant_id": GLOBAL_TENANT_ID, "connector_id": connector_id},
            )
            item = response.get("Item")
        if item is None:
            return None
        return ConnectorRecord(**decimal_to_native(item))

    async def create_connector(
        self,
        tenant_id: str,
        name: str,
        executor_type: ExecutorType,
        description: str,
        created_by: str,
        input_schema: dict[str, Any] | None = None,
        output_schema: dict[str, Any] | None = None,
        endpoint_template: str | None = None,
        credentials_required: list[str] | None = None,
    ) -> ConnectorRecord:
        connector_id = f"{_slugify(name)}-{uuid.uuid4().hex[:6]}"
        record = ConnectorRecord(
            tenant_id=tenant_id,
            connector_id=connector_id,
            name=name,
            executor_type=executor_type,
            description=description,
            input_schema=input_schema or {},
            output_schema=output_schema or {},
            endpoint_template=endpoint_template,
            credentials_required=credentials_required or [],
            is_global=False,
            created_by=created_by,
            created_at=_now(),
        )
        await asyncio.to_thread(self._table.put_item, Item=record.model_dump(mode="json"))
        return record

    async def get_tenant_owned_connector(
        self, tenant_id: str, connector_id: str
    ) -> ConnectorRecord | None:
        """Like get_connector, but scoped ONLY to the caller's own tenant
        partition — never falls back to GLOBAL_TENANT_ID. Global connectors
        are Panasa-curated (seed_global_connectors, re-applied at every
        startup) and must never be edited or deleted by a tenant; using this
        instead of get_connector for update/delete is what enforces that,
        rather than a separate is_global check after the fact."""
        response = await asyncio.to_thread(
            self._table.get_item, Key={"tenant_id": tenant_id, "connector_id": connector_id}
        )
        item = response.get("Item")
        if item is None:
            return None
        return ConnectorRecord(**decimal_to_native(item))

    async def update_connector(
        self,
        tenant_id: str,
        connector_id: str,
        name: str,
        executor_type: ExecutorType,
        description: str,
        input_schema: dict[str, Any] | None = None,
        output_schema: dict[str, Any] | None = None,
        endpoint_template: str | None = None,
        credentials_required: list[str] | None = None,
    ) -> ConnectorRecord | None:
        existing = await self.get_tenant_owned_connector(tenant_id, connector_id)
        if existing is None:
            return None
        updated = existing.model_copy(
            update={
                "name": name,
                "executor_type": executor_type,
                "description": description,
                "input_schema": input_schema if input_schema is not None else existing.input_schema,
                "output_schema": output_schema
                if output_schema is not None
                else existing.output_schema,
                "endpoint_template": endpoint_template,
                "credentials_required": credentials_required
                if credentials_required is not None
                else existing.credentials_required,
            }
        )
        await asyncio.to_thread(self._table.put_item, Item=updated.model_dump(mode="json"))
        return updated

    async def delete_connector(self, tenant_id: str, connector_id: str) -> bool:
        existing = await self.get_tenant_owned_connector(tenant_id, connector_id)
        if existing is None:
            return False
        await asyncio.to_thread(
            self._table.delete_item, Key={"tenant_id": tenant_id, "connector_id": connector_id}
        )
        return True
