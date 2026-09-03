"""Knowledge Base retrieval via Bedrock Agent Runtime.

KBConfig.kb_id (this agent's own config) references a row in the Factory
Runtime's knowledge base catalog (panasa-knowledge-bases, keyed by
{tenant_id, kb_id}) — that catalog record's bedrock_kb_id is the real,
AWS-provisioned Knowledge Base this queries. The KB itself is provisioned
by the Factory Runtime's own API directly against Bedrock
(app/modules/knowledge_base/provisioner.py) when it's created/synced, not
by this agent's own Terraform (rag.tf.j2's aws_bedrockagent_knowledge_base
exists for the IaC validation suite's structural checks — R03/F0 mean this
runtime never reads Terraform state to discover the real one, so it reads
the same DynamoDB catalog row the Factory Console's own KB pages do).
"""

from __future__ import annotations

import asyncio
import os
from typing import Any

import boto3


class RAGClient:
    def __init__(
        self,
        tenant_id: str,
        kb_config: dict[str, Any] | None,
        dynamodb: Any | None = None,
        bedrock_agent_runtime: Any | None = None,
    ) -> None:
        self._tenant_id = tenant_id
        self._kb_config = kb_config or {}
        self._top_k = int(self._kb_config.get("top_k") or 5)
        self._kb_id = self._kb_config.get("kb_id")
        region = os.environ.get("AWS_REGION", "eu-west-2")
        self._dynamodb = dynamodb or boto3.resource("dynamodb", region_name=region)
        self._client = bedrock_agent_runtime or boto3.client(
            "bedrock-agent-runtime", region_name=region
        )
        self._kb_catalog_table = os.environ.get(
            "DYNAMODB_KNOWLEDGE_BASES_TABLE", "panasa-knowledge-bases"
        )
        self._bedrock_kb_id: str | None = None
        self._resolved = False

    @property
    def enabled(self) -> bool:
        return bool(self._kb_config.get("enabled")) and bool(self._kb_id)

    async def _resolve_bedrock_kb_id(self) -> str | None:
        if self._resolved:
            return self._bedrock_kb_id
        self._resolved = True
        if not self._kb_id:
            return None
        table = self._dynamodb.Table(self._kb_catalog_table)
        response = await asyncio.to_thread(
            table.get_item, Key={"tenant_id": self._tenant_id, "kb_id": self._kb_id}
        )
        item = response.get("Item")
        self._bedrock_kb_id = item.get("bedrock_kb_id") if item else None
        return self._bedrock_kb_id

    async def retrieve(self, query: str) -> str:
        if not self.enabled:
            return ""
        bedrock_kb_id = await self._resolve_bedrock_kb_id()
        if not bedrock_kb_id:
            return ""

        response = await asyncio.to_thread(
            self._client.retrieve,
            knowledgeBaseId=bedrock_kb_id,
            retrievalQuery={"text": query},
            retrievalConfiguration={
                "vectorSearchConfiguration": {"numberOfResults": self._top_k}
            },
        )
        chunks = [
            result["content"]["text"]
            for result in response.get("retrievalResults", [])
            if result.get("content", {}).get("text")
        ]
        return "\n\n".join(chunks)
