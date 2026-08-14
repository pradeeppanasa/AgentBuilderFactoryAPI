"""Knowledge Base library models (CLAUDE_Advanced_Config.md Section 3.3 / 4.1).

Distinct from the per-agent `app.modules.registry.models.KBConfig` (inline
retrieval-tuning overrides referenced by `AgentConfiguration.knowledge_base`)
— a `KnowledgeBaseRecord` is the library-managed *source* (where the
content comes from, how it's chunked/embedded); `KBConfig`/the new
`AgentConfiguration.kb_id` reference one of these by id.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

KBSourceType = Literal["s3", "url", "upload", "manual"]
KBStatus = Literal["INDEXING", "READY", "FAILED"]

EmbeddingModel = Literal[
    "amazon.titan-embed-text-v2:0",
    "cohere.embed-english-v3",
    "cohere.embed-multilingual-v3",
]


class KnowledgeBaseRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kb_id: str
    tenant_id: str
    name: str
    description: str

    source_type: KBSourceType
    source_config: dict[str, Any] = Field(default_factory=dict)
    """e.g. bucket/prefix for "s3", a list of URLs for "url" — shape depends
    on source_type; deliberately untyped here, same as ToolConfig.input_schema
    elsewhere in this codebase."""

    embedding_model: EmbeddingModel = "amazon.titan-embed-text-v2:0"
    chunk_size_tokens: int = 512
    chunk_overlap_pct: int = 10

    status: KBStatus = "INDEXING"
    document_count: int = 0

    created_by: str
    created_at: str
    updated_at: str
