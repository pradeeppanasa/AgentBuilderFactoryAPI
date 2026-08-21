"""Knowledge Base library — DynamoDB CRUD (CLAUDE_Advanced_Config.md
Section 4.1/8). Tenant-scoped only — unlike the connector catalog, the KB
library has no GLOBAL partition concept in the spec.
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
from app.modules.knowledge_base.models import (
    EmbeddingModel,
    KBSourceType,
    KBSyncStatus,
    KnowledgeBaseRecord,
)
from app.modules.knowledge_base.provisioner import BedrockKnowledgeBaseProvisioner
from app.shared.dynamodb_types import decimal_to_native


def _slugify(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return slug or "kb"


def _now() -> str:
    return datetime.now(UTC).isoformat()


class KnowledgeBaseNotFoundError(Exception):
    def __init__(self, kb_id: str) -> None:
        self.kb_id = kb_id
        super().__init__(f"Knowledge base {kb_id!r} not found")


class KnowledgeBaseStore:
    def __init__(self, dynamodb_resource: Any, settings: Settings) -> None:
        self._dynamodb = dynamodb_resource
        self._settings = settings
        self._table = dynamodb_resource.Table(settings.dynamodb_knowledge_bases_table)

    async def ensure_table(self) -> None:
        def _create() -> None:
            try:
                table = self._dynamodb.create_table(
                    TableName=self._settings.dynamodb_knowledge_bases_table,
                    KeySchema=[
                        {"AttributeName": "tenant_id", "KeyType": "HASH"},
                        {"AttributeName": "kb_id", "KeyType": "RANGE"},
                    ],
                    AttributeDefinitions=[
                        {"AttributeName": "tenant_id", "AttributeType": "S"},
                        {"AttributeName": "kb_id", "AttributeType": "S"},
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
        source_type: KBSourceType,
        created_by: str,
        source_config: dict[str, Any] | None = None,
        embedding_model: EmbeddingModel = "amazon.titan-embed-text-v2:0",
        chunk_size_tokens: int = 512,
        chunk_overlap_pct: int = 10,
        chunk_strategy: str = "semantic",
        kb_documents_bucket: str | None = None,
        provisioner: BedrockKnowledgeBaseProvisioner | None = None,
    ) -> KnowledgeBaseRecord:
        """When `provisioner` is given (instructions_kb_api.md /
        CLAUDE.md Section 43), also provisions a real Bedrock Knowledge
        Base + S3 data source and stores the resulting bedrock_kb_id/
        bedrock_ds_id/s3_bucket/s3_prefix. A provisioning failure
        (KnowledgeBaseProvisioningError) propagates to the caller before
        anything is written to DynamoDB — never a half-created record."""
        now = _now()
        kb_id = f"{_slugify(name)}-{uuid.uuid4().hex[:6]}"
        s3_prefix = f"{tenant_id}/{kb_id}/raw/" if kb_documents_bucket else None

        record = KnowledgeBaseRecord(
            kb_id=kb_id,
            tenant_id=tenant_id,
            name=name,
            description=description,
            source_type=source_type,
            source_config=source_config or {},
            embedding_model=embedding_model,
            chunk_size_tokens=chunk_size_tokens,
            chunk_overlap_pct=chunk_overlap_pct,
            chunk_strategy=chunk_strategy,
            status="CREATING" if provisioner is not None else "INDEXING",
            document_count=0,
            s3_bucket=kb_documents_bucket,
            s3_prefix=s3_prefix,
            created_by=created_by,
            created_at=now,
            updated_at=now,
        )

        if provisioner is not None:
            bedrock_kb_id, bedrock_ds_id = await provisioner.provision(record)
            record = record.model_copy(
                update={
                    "bedrock_kb_id": bedrock_kb_id,
                    "bedrock_ds_id": bedrock_ds_id,
                    "status": "ACTIVE",
                }
            )

        await asyncio.to_thread(self._table.put_item, Item=record.model_dump(mode="json"))
        return record

    async def update_sync_state(
        self,
        tenant_id: str,
        kb_id: str,
        sync_status: KBSyncStatus,
        sync_error: str | None = None,
        mark_synced: bool = False,
    ) -> KnowledgeBaseRecord:
        record = await self.get(tenant_id, kb_id)
        if record is None:
            raise KnowledgeBaseNotFoundError(kb_id)
        updates: dict[str, Any] = {
            "sync_status": sync_status,
            "sync_error": sync_error,
            "status": "SYNCING" if sync_status == "IN_PROGRESS" else "ACTIVE",
            "updated_at": _now(),
        }
        if mark_synced:
            updates["last_synced_at"] = _now()
        updated = record.model_copy(update=updates)
        await asyncio.to_thread(self._table.put_item, Item=updated.model_dump(mode="json"))
        return updated

    async def set_document_count(
        self, tenant_id: str, kb_id: str, document_count: int
    ) -> KnowledgeBaseRecord:
        record = await self.get(tenant_id, kb_id)
        if record is None:
            raise KnowledgeBaseNotFoundError(kb_id)
        updated = record.model_copy(
            update={"document_count": document_count, "updated_at": _now()}
        )
        await asyncio.to_thread(self._table.put_item, Item=updated.model_dump(mode="json"))
        return updated

    async def list_knowledge_bases(self, tenant_id: str) -> list[KnowledgeBaseRecord]:
        response = await asyncio.to_thread(
            self._table.query, KeyConditionExpression=Key("tenant_id").eq(tenant_id)
        )
        return [
            KnowledgeBaseRecord(**decimal_to_native(item)) for item in response.get("Items", [])
        ]

    async def get(self, tenant_id: str, kb_id: str) -> KnowledgeBaseRecord | None:
        response = await asyncio.to_thread(
            self._table.get_item, Key={"tenant_id": tenant_id, "kb_id": kb_id}
        )
        item = response.get("Item")
        return KnowledgeBaseRecord(**decimal_to_native(item)) if item else None

    async def delete(self, tenant_id: str, kb_id: str) -> None:
        await asyncio.to_thread(
            self._table.delete_item, Key={"tenant_id": tenant_id, "kb_id": kb_id}
        )

    async def trigger_reindex(self, tenant_id: str, kb_id: str) -> KnowledgeBaseRecord:
        """Sets status back to INDEXING. Actually re-crawling/re-embedding the
        source is real indexing infrastructure this Runtime doesn't build
        (same "Generated Agent Runtime is a separate service" boundary, F8)
        — a real deployment would have something external drive status back
        to READY/FAILED once the reindex completes; this just records the
        request and flips the visible status."""
        record = await self.get(tenant_id, kb_id)
        if record is None:
            raise KnowledgeBaseNotFoundError(kb_id)
        updated = record.model_copy(update={"status": "INDEXING", "updated_at": _now()})
        await asyncio.to_thread(self._table.put_item, Item=updated.model_dump(mode="json"))
        return updated
