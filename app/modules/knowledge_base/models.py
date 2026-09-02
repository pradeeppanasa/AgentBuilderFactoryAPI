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
# CREATING/ACTIVE/SYNCING/SYNC_FAILED/DELETING added alongside the original
# INDEXING/READY/FAILED (CLAUDE.md Section 43/instructions_kb_api.md,
# 2026-08-19) rather than replacing them — existing records and the
# pre-Bedrock reindex stub still use the old three; real Bedrock-backed KBs
# (see provisioner.py) use the new five. Additive, same treatment every
# other Advanced Config extension in this codebase gets.
KBStatus = Literal[
    "INDEXING", "READY", "FAILED", "CREATING", "ACTIVE", "SYNCING", "SYNC_FAILED", "DELETING"
]
KBSyncStatus = Literal["IN_PROGRESS", "COMPLETE", "FAILED"]

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
    chunk_strategy: Literal["semantic", "fixed", "paragraph"] = "semantic"
    """Bedrock's own chunking-strategy concept (instructions_kb_api.md) —
    distinct from chunk_size_tokens/chunk_overlap_pct above, which predate
    real Bedrock provisioning and describe a size-based scheme Bedrock's
    SEMANTIC/FIXED_SIZE chunking strategies don't directly map onto."""

    status: KBStatus = "INDEXING"
    document_count: int = 0

    # ── Real Bedrock provisioning (Section 43 / instructions_kb_api.md,
    # 2026-08-19) — additive. None for any KB created before this existed,
    # or one whose source_type isn't backed by a real Bedrock KB yet.
    s3_bucket: str | None = None
    s3_prefix: str | None = None
    """"{s3_folder_prefix}/{kb_id}/raw/" for Panasa-managed storage, or the
    customer's own path verbatim for source_type="s3" (CLAUDE.md Section 47,
    R59 corrected 2026-09-01) — never "panasa", never the tenant_id."""
    bedrock_kb_id: str | None = None
    bedrock_ds_id: str | None = None
    last_synced_at: str | None = None
    sync_status: KBSyncStatus | None = None
    sync_error: str | None = None

    created_by: str
    created_at: str
    updated_at: str
