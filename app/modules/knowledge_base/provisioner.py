"""Provisions the real AWS Bedrock Knowledge Base + S3 data source behind a
KnowledgeBaseRecord (instructions_kb_api.md / CLAUDE.md Section 43).

Same "provision at the API layer, store persists the result" composition as
app.modules.guardrails.provisioner.BedrockGuardrailProvisioner — this class
only talks to Bedrock, it never touches DynamoDB itself.

Bedrock KB creation and ingestion-job control are `bedrock-agent` control-
plane operations moto does not implement (confirmed against the guardrail
provisioner's own note on create_guardrail/update_guardrail — the same gap
applies here); tests inject a fake client the same way
test_task_planner_architecture_api.py's FakeBedrockControlPlaneClient does
for guardrails.
"""

from __future__ import annotations

import asyncio
from typing import Any

from app.modules.knowledge_base.models import KnowledgeBaseRecord


class KnowledgeBaseProvisioningError(Exception):
    """Bedrock KB/data-source create or delete failed — mapped to a 503 by
    the API layer (instructions_kb_api.md's error table), never a raw
    boto3/botocore exception."""


class BedrockKnowledgeBaseProvisioner:
    def __init__(
        self,
        bedrock_agent_client: Any,
        kb_role_arn: str,
        opensearch_collection_arn: str,
        aws_region: str,
        kb_documents_bucket: str,
        mock_enabled: bool = False,
    ) -> None:
        self._client = bedrock_agent_client
        self._kb_role_arn = kb_role_arn
        self._opensearch_collection_arn = opensearch_collection_arn
        self._aws_region = aws_region
        self._kb_documents_bucket = kb_documents_bucket
        self._mock_enabled = mock_enabled

    async def provision(self, kb: KnowledgeBaseRecord) -> tuple[str, str]:
        """Creates the Bedrock KB and its S3 data source. Returns
        (bedrock_kb_id, bedrock_ds_id). Never updates an existing KB in
        place — chunk_strategy/embedding_model changes are Bedrock-side
        immutable-after-create properties for a data source, so a change
        there means deleting and recreating, not a PATCH; that policy
        decision belongs to the API layer, not this provisioner.

        The data source's S3 bucket is always `kb.s3_bucket` — the tenant's
        own configured bucket (Section 47, R59 corrected 2026-09-01),
        resolved per-request by the API layer before store.create() built
        this record. `self._kb_documents_bucket` is only a startup-time
        fallback default for local dev, never used here directly."""
        if self._mock_enabled:
            return f"mock-bedrock-kb-{kb.kb_id}", f"mock-bedrock-ds-{kb.kb_id}"

        try:
            kb_response = await asyncio.to_thread(
                self._client.create_knowledge_base,
                name=f"panasa-{kb.tenant_id}-{kb.kb_id}",
                description=kb.description or "",
                roleArn=self._kb_role_arn,
                knowledgeBaseConfiguration={
                    "type": "VECTOR",
                    "vectorKnowledgeBaseConfiguration": {
                        "embeddingModelArn": (
                            f"arn:aws:bedrock:{self._aws_region}::foundation-model/"
                            f"{kb.embedding_model}"
                        )
                    },
                },
                storageConfiguration={
                    "type": "OPENSEARCH_SERVERLESS",
                    "opensearchServerlessConfiguration": {
                        "collectionArn": self._opensearch_collection_arn,
                        "vectorIndexName": f"panasa-{kb.kb_id}-index",
                        "fieldMapping": {
                            "vectorField": "embedding",
                            "textField": "text",
                            "metadataField": "metadata",
                        },
                    },
                },
            )
            bedrock_kb_id = kb_response["knowledgeBase"]["knowledgeBaseId"]

            ds_response = await asyncio.to_thread(
                self._client.create_data_source,
                knowledgeBaseId=bedrock_kb_id,
                name=f"panasa-{kb.kb_id}-s3-source",
                dataSourceConfiguration={
                    "type": "S3",
                    "s3Configuration": {
                        "bucketArn": f"arn:aws:s3:::{kb.s3_bucket}",
                        "inclusionPrefixes": [kb.s3_prefix],
                    },
                },
                vectorIngestionConfiguration={
                    "chunkingConfiguration": {"chunkingStrategy": kb.chunk_strategy.upper()}
                },
            )
            return bedrock_kb_id, ds_response["dataSource"]["dataSourceId"]
        except Exception as exc:
            raise KnowledgeBaseProvisioningError(
                f"Could not create knowledge base: {exc}"
            ) from exc

    async def deprovision(self, kb: KnowledgeBaseRecord) -> None:
        """Best-effort, same reasoning as BedrockGuardrailProvisioner.
        deprovision — the DynamoDB record is this Runtime's source of
        truth (R02); a stray orphaned Bedrock resource is a cheaper
        failure mode than blocking the delete the admin asked for."""
        if self._mock_enabled or not kb.bedrock_kb_id:
            return
        try:
            if kb.bedrock_ds_id:
                await asyncio.to_thread(
                    self._client.delete_data_source,
                    knowledgeBaseId=kb.bedrock_kb_id,
                    dataSourceId=kb.bedrock_ds_id,
                )
            await asyncio.to_thread(
                self._client.delete_knowledge_base, knowledgeBaseId=kb.bedrock_kb_id
            )
        except Exception:
            pass

    async def start_sync(self, kb: KnowledgeBaseRecord) -> str:
        """Triggers a Bedrock ingestion job. Returns ingestion_job_id."""
        if self._mock_enabled:
            return f"mock-ingestion-{kb.kb_id}"
        if not kb.bedrock_kb_id or not kb.bedrock_ds_id:
            raise KnowledgeBaseProvisioningError(
                "Knowledge base has no provisioned Bedrock resource to sync."
            )
        try:
            response = await asyncio.to_thread(
                self._client.start_ingestion_job,
                knowledgeBaseId=kb.bedrock_kb_id,
                dataSourceId=kb.bedrock_ds_id,
            )
            return str(response["ingestionJob"]["ingestionJobId"])
        except Exception as exc:
            raise KnowledgeBaseProvisioningError(f"Could not start sync: {exc}") from exc

    async def get_sync_status(
        self, kb: KnowledgeBaseRecord, ingestion_job_id: str
    ) -> dict[str, Any]:
        if self._mock_enabled:
            return {
                "status": "COMPLETE",
                "documents_indexed": kb.document_count,
                "documents_failed": 0,
                "started_at": None,
                "updated_at": None,
                "error": None,
            }
        if not kb.bedrock_kb_id or not kb.bedrock_ds_id:
            raise KnowledgeBaseProvisioningError(
                "Knowledge base has no provisioned Bedrock resource to check."
            )
        try:
            response = await asyncio.to_thread(
                self._client.get_ingestion_job,
                knowledgeBaseId=kb.bedrock_kb_id,
                dataSourceId=kb.bedrock_ds_id,
                ingestionJobId=ingestion_job_id,
            )
        except Exception as exc:
            raise KnowledgeBaseProvisioningError(f"Could not check sync status: {exc}") from exc

        job = response["ingestionJob"]
        stats = job.get("statistics", {})
        failure_reasons = job.get("failureReasons") or []
        return {
            "status": job["status"],
            "documents_indexed": stats.get("numberOfDocumentsIndexed", 0),
            "documents_failed": stats.get("numberOfDocumentsFailed", 0),
            "started_at": job.get("startedAt"),
            "updated_at": job.get("updatedAt"),
            "error": failure_reasons[0] if failure_reasons else None,
        }
