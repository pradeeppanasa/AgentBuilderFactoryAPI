"""Knowledge Base library (CLAUDE_Advanced_Config.md Section 4.1 / 5).

Document upload/list/delete and sync trigger/status
(instructions_kb_api.md / CLAUDE.md Section 43, 2026-08-19) are additive to
the original library CRUD below — real S3 + Bedrock provisioning only
kicks in when a bucket is configured (Section 47, R59 corrected
2026-09-01: the tenant's own "Settings -> Deployment -> Customer S3
Bucket", falling back to `settings.kb_documents_bucket` for local dev);
with neither set, `create_knowledge_base` behaves exactly as before
(DynamoDB-only, no S3/Bedrock calls).

A KB is a standalone platform resource — created, configured, and synced
independently of any agent's deployment lifecycle. There is deliberately
NO "agent must be ACTIVE" gate anywhere in this file: `_agents_referencing_kb`
below exists only for the delete guard (can't delete a KB still attached to
an agent), not to gate uploads.
"""

from __future__ import annotations

import asyncio
from pathlib import PurePosixPath
from typing import Annotated, Any, Literal
from urllib.parse import unquote

from botocore.exceptions import ClientError
from fastapi import APIRouter, Depends, HTTPException, Query, UploadFile, status
from pydantic import BaseModel

from app.config import settings
from app.dependencies import (
    get_bedrock_kb_provisioner,
    get_knowledge_base_store,
    get_platform_settings_store,
    get_registry_store,
    get_s3_client,
    get_tenant_id,
)
from app.modules.auth.dependencies import require_role
from app.modules.auth.schemas import CurrentUser
from app.modules.knowledge_base.models import EmbeddingModel, KBSourceType, KnowledgeBaseRecord
from app.modules.knowledge_base.provisioner import (
    BedrockKnowledgeBaseProvisioner,
    KnowledgeBaseProvisioningError,
)
from app.modules.knowledge_base.store import (
    InvalidSourceConfigError,
    KnowledgeBaseNotFoundError,
    KnowledgeBaseStore,
)
from app.modules.platform_settings.store import PlatformSettingsStore
from app.modules.registry.store import AgentRegistryStore
from app.shared.logging import get_logger

router = APIRouter(prefix="/platform/knowledge-bases", tags=["knowledge-bases"])
log = get_logger()

_READ_ROLES = ("developer", "analyst", "auditor")
_WRITE_ROLES = ("developer",)

# instructions_kb_api.md's exact supported-file-type list.
_ALLOWED_DOCUMENT_EXTENSIONS = {".pdf", ".docx", ".doc", ".txt", ".md", ".html", ".csv"}

_NOT_CONFIGURED_MESSAGE = "Configure your S3 bucket in Settings → Deployment first."


async def _resolve_kb_bucket(
    tenant_id: str, platform_settings_store: PlatformSettingsStore
) -> tuple[str | None, str]:
    """(bucket, prefix) for this tenant's KB uploads. The tenant's own
    "Settings -> Deployment -> Customer S3 Bucket" always wins; the global
    KB_DOCUMENTS_BUCKET env var is only a fallback default for local/
    Prototype-mode convenience where there's no per-tenant Settings UI in
    play. The prefix always comes from tenant settings (defaults to
    "agent-factory") regardless of which bucket source is used."""
    record = await platform_settings_store.get_or_create(tenant_id, "system")
    bucket = record.kb_s3_bucket or settings.kb_documents_bucket
    return bucket, record.kb_s3_prefix


class KnowledgeBaseListResponse(BaseModel):
    items: list[KnowledgeBaseRecord]


class CreateKnowledgeBaseRequest(BaseModel):
    name: str
    description: str
    source_type: KBSourceType = "manual"
    source_config: dict[str, Any] = {}
    embedding_model: EmbeddingModel = "amazon.titan-embed-text-v2:0"
    chunk_size_tokens: int = 512
    chunk_overlap_pct: int = 10
    chunk_strategy: Literal["semantic", "fixed", "paragraph"] = "semantic"


async def _agents_referencing_kb(
    registry_store: AgentRegistryStore, tenant_id: str, kb_id: str
) -> list[str]:
    """Full-tenant scan, same pattern as lambda_handlers/validating.py's
    _build_sub_agent_graph — acceptable here for the same reason: an
    admin-triggered delete-check, not a hot path. Used ONLY by the delete
    guard below — a KB is a standalone resource with no other dependency
    on which/whether agents reference it (Section 47, R59 corrected
    2026-09-01: there is no "agent must be deployed" gate anywhere else in
    this file)."""
    referencing: list[str] = []
    cursor: str | None = None
    while True:
        records, cursor = await registry_store.list_agents(tenant_id, limit=100, cursor=cursor)
        for record in records:
            try:
                version = await registry_store.get_version(record.agent_id, record.current_version)
            except Exception:
                log.warning(
                    "kb.referencing_agents.version_read_failed",
                    agent_id=record.agent_id,
                    exc_info=True,
                )
                continue
            if version is not None and version.configuration.kb_id == kb_id:
                referencing.append(record.agent_id)
        if cursor is None:
            return referencing


async def _get_kb_or_404(
    store: KnowledgeBaseStore, tenant_id: str, kb_id: str
) -> KnowledgeBaseRecord:
    record = await store.get(tenant_id, kb_id)
    if record is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"Knowledge base {kb_id!r} not found"
        )
    return record


@router.get("", response_model=KnowledgeBaseListResponse)
async def list_knowledge_bases(
    tenant_id: Annotated[str, Depends(get_tenant_id)],
    _current_user: Annotated[CurrentUser, Depends(require_role(*_READ_ROLES))],
    store: Annotated[KnowledgeBaseStore, Depends(get_knowledge_base_store)],
) -> KnowledgeBaseListResponse:
    return KnowledgeBaseListResponse(items=await store.list_knowledge_bases(tenant_id))


@router.post("", response_model=KnowledgeBaseRecord, status_code=status.HTTP_201_CREATED)
async def create_knowledge_base(
    payload: CreateKnowledgeBaseRequest,
    tenant_id: Annotated[str, Depends(get_tenant_id)],
    current_user: Annotated[CurrentUser, Depends(require_role(*_WRITE_ROLES))],
    store: Annotated[KnowledgeBaseStore, Depends(get_knowledge_base_store)],
    provisioner: Annotated[BedrockKnowledgeBaseProvisioner, Depends(get_bedrock_kb_provisioner)],
    platform_settings_store: Annotated[
        PlatformSettingsStore, Depends(get_platform_settings_store)
    ],
) -> KnowledgeBaseRecord:
    # Real Bedrock/S3 provisioning only once a bucket is configured (this
    # tenant's Settings, or the local-dev env var fallback) — otherwise
    # this behaves exactly as before (DynamoDB-only, source_type driven).
    bucket, prefix = await _resolve_kb_bucket(tenant_id, platform_settings_store)
    use_provisioner = bucket is not None
    try:
        return await store.create(
            tenant_id=tenant_id,
            name=payload.name,
            description=payload.description,
            source_type=payload.source_type,
            created_by=current_user.email,
            source_config=payload.source_config,
            embedding_model=payload.embedding_model,
            chunk_size_tokens=payload.chunk_size_tokens,
            chunk_overlap_pct=payload.chunk_overlap_pct,
            chunk_strategy=payload.chunk_strategy,
            kb_documents_bucket=bucket if use_provisioner else None,
            kb_s3_prefix=prefix,
            provisioner=provisioner if use_provisioner else None,
        )
    except KnowledgeBaseProvisioningError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"error": "bedrock_unavailable", "message": str(exc)},
        ) from exc
    except InvalidSourceConfigError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"error": "invalid_source_config", "message": str(exc)},
        ) from exc


@router.get("/{kb_id}", response_model=KnowledgeBaseRecord)
async def get_knowledge_base(
    kb_id: str,
    tenant_id: Annotated[str, Depends(get_tenant_id)],
    _current_user: Annotated[CurrentUser, Depends(require_role(*_READ_ROLES))],
    store: Annotated[KnowledgeBaseStore, Depends(get_knowledge_base_store)],
) -> KnowledgeBaseRecord:
    return await _get_kb_or_404(store, tenant_id, kb_id)


@router.delete("/{kb_id}", status_code=status.HTTP_204_NO_CONTENT, response_model=None)
async def delete_knowledge_base(
    kb_id: str,
    tenant_id: Annotated[str, Depends(get_tenant_id)],
    _current_user: Annotated[CurrentUser, Depends(require_role(*_WRITE_ROLES))],
    store: Annotated[KnowledgeBaseStore, Depends(get_knowledge_base_store)],
    registry_store: Annotated[AgentRegistryStore, Depends(get_registry_store)],
    provisioner: Annotated[BedrockKnowledgeBaseProvisioner, Depends(get_bedrock_kb_provisioner)],
    s3_client: Annotated[Any, Depends(get_s3_client)],
) -> None:
    record = await _get_kb_or_404(store, tenant_id, kb_id)
    referencing = await _agents_referencing_kb(registry_store, tenant_id, kb_id)
    if referencing:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"Knowledge base {kb_id!r} is still referenced by agent(s): "
                f"{', '.join(referencing)}"
            ),
        )

    # instructions_kb_api.md's delete order: Bedrock data source -> Bedrock
    # KB -> S3 objects -> DynamoDB record.
    await provisioner.deprovision(record)
    if record.s3_bucket and record.s3_prefix:
        await _delete_all_under_prefix(s3_client, record.s3_bucket, record.s3_prefix)
    await store.delete(tenant_id, kb_id)


@router.post("/{kb_id}/reindex", response_model=KnowledgeBaseRecord)
async def reindex_knowledge_base(
    kb_id: str,
    tenant_id: Annotated[str, Depends(get_tenant_id)],
    _current_user: Annotated[CurrentUser, Depends(require_role(*_WRITE_ROLES))],
    store: Annotated[KnowledgeBaseStore, Depends(get_knowledge_base_store)],
) -> KnowledgeBaseRecord:
    try:
        return await store.trigger_reindex(tenant_id, kb_id)
    except KnowledgeBaseNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc


# ── Document upload / list / delete (instructions_kb_api.md) ───────────────


class UploadedDocumentSummary(BaseModel):
    filename: str
    s3_key: str
    size_bytes: int


class UploadDocumentsResponse(BaseModel):
    uploaded: list[UploadedDocumentSummary]
    count: int


class DocumentSummary(BaseModel):
    filename: str
    s3_key: str
    size_bytes: int
    last_modified: str


class DocumentListResponse(BaseModel):
    documents: list[DocumentSummary]
    count: int


def _document_s3_key(kb: KnowledgeBaseRecord, filename: str, subfolder: str | None) -> str:
    assert kb.s3_prefix is not None  # guarded by _require_provisioned in the route below
    if subfolder:
        return f"{kb.s3_prefix}{subfolder.strip('/')}/{filename}"
    return f"{kb.s3_prefix}{filename}"


def _require_provisioned(kb: KnowledgeBaseRecord) -> None:
    # Section 47 (R59 corrected 2026-09-01): this KB was created before a
    # bucket was configured (or the tenant never configured one) — nothing
    # to do with agent deployment. 409, not 503: this is an expected,
    # actionable "not set up yet" state, not an outage.
    if not kb.s3_bucket or not kb.s3_prefix:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"error": "s3_not_configured", "message": _NOT_CONFIGURED_MESSAGE},
        )


@router.post("/{kb_id}/documents", response_model=UploadDocumentsResponse)
async def upload_documents(
    kb_id: str,
    files: list[UploadFile],
    tenant_id: Annotated[str, Depends(get_tenant_id)],
    _current_user: Annotated[CurrentUser, Depends(require_role(*_WRITE_ROLES))],
    store: Annotated[KnowledgeBaseStore, Depends(get_knowledge_base_store)],
    s3_client: Annotated[Any, Depends(get_s3_client)],
    subfolder: str | None = None,
) -> UploadDocumentsResponse:
    kb = await _get_kb_or_404(store, tenant_id, kb_id)
    _require_provisioned(kb)

    for file in files:
        ext = PurePosixPath(file.filename or "").suffix.lower()
        if ext not in _ALLOWED_DOCUMENT_EXTENSIONS:
            raise HTTPException(
                status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
                detail={
                    "error": "unsupported_file_type",
                    "message": (
                        f"File type {ext!r} is not supported. Allowed: "
                        + ", ".join(sorted(e.lstrip(".") for e in _ALLOWED_DOCUMENT_EXTENSIONS))
                    ),
                },
            )

    uploaded: list[UploadedDocumentSummary] = []
    try:
        for file in files:
            filename = file.filename or "document"
            s3_key = _document_s3_key(kb, filename, subfolder)
            content = await file.read()
            await asyncio.to_thread(
                s3_client.put_object,
                Bucket=kb.s3_bucket,
                Key=s3_key,
                Body=content,
                ContentType=file.content_type or "application/octet-stream",
            )
            uploaded.append(
                UploadedDocumentSummary(filename=filename, s3_key=s3_key, size_bytes=len(content))
            )
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "error": "storage_unavailable",
                "message": f"Could not upload file: {exc}",
            },
        ) from exc

    await store.set_document_count(tenant_id, kb_id, kb.document_count + len(uploaded))
    return UploadDocumentsResponse(uploaded=uploaded, count=len(uploaded))


@router.get("/{kb_id}/documents", response_model=DocumentListResponse)
async def list_documents(
    kb_id: str,
    tenant_id: Annotated[str, Depends(get_tenant_id)],
    _current_user: Annotated[CurrentUser, Depends(require_role(*_READ_ROLES))],
    store: Annotated[KnowledgeBaseStore, Depends(get_knowledge_base_store)],
    s3_client: Annotated[Any, Depends(get_s3_client)],
) -> DocumentListResponse:
    kb = await _get_kb_or_404(store, tenant_id, kb_id)
    _require_provisioned(kb)

    response = await asyncio.to_thread(
        s3_client.list_objects_v2, Bucket=kb.s3_bucket, Prefix=kb.s3_prefix
    )
    documents = [
        DocumentSummary(
            filename=obj["Key"][len(kb.s3_prefix or "") :],
            s3_key=obj["Key"],
            size_bytes=obj["Size"],
            last_modified=obj["LastModified"].isoformat()
            if hasattr(obj["LastModified"], "isoformat")
            else str(obj["LastModified"]),
        )
        for obj in response.get("Contents", [])
    ]
    return DocumentListResponse(documents=documents, count=len(documents))


@router.delete(
    "/{kb_id}/documents/{s3_key:path}", status_code=status.HTTP_204_NO_CONTENT, response_model=None
)
async def delete_document(
    kb_id: str,
    s3_key: str,
    tenant_id: Annotated[str, Depends(get_tenant_id)],
    _current_user: Annotated[CurrentUser, Depends(require_role(*_WRITE_ROLES))],
    store: Annotated[KnowledgeBaseStore, Depends(get_knowledge_base_store)],
    s3_client: Annotated[Any, Depends(get_s3_client)],
) -> None:
    kb = await _get_kb_or_404(store, tenant_id, kb_id)
    _require_provisioned(kb)

    decoded_key = unquote(s3_key)
    if not decoded_key.startswith(kb.s3_prefix or "\0"):
        # Never allow deleting outside this KB's own prefix, regardless of
        # what key the caller passes in.
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Document not found")

    await asyncio.to_thread(s3_client.delete_object, Bucket=kb.s3_bucket, Key=decoded_key)
    await store.set_document_count(tenant_id, kb_id, max(0, kb.document_count - 1))


async def _delete_all_under_prefix(s3_client: Any, bucket: str, prefix: str) -> None:
    response = await asyncio.to_thread(s3_client.list_objects_v2, Bucket=bucket, Prefix=prefix)
    keys = [{"Key": obj["Key"]} for obj in response.get("Contents", [])]
    if keys:
        await asyncio.to_thread(s3_client.delete_objects, Bucket=bucket, Delete={"Objects": keys})


# ── Sync (instructions_kb_api.md) ───────────────────────────────────────


class SyncTriggerResponse(BaseModel):
    ingestion_job_id: str
    status: str


class SyncStatusResponse(BaseModel):
    status: str
    documents_indexed: int
    documents_failed: int
    started_at: str | None
    updated_at: str | None
    error: str | None


@router.post(
    "/{kb_id}/sync", response_model=SyncTriggerResponse, status_code=status.HTTP_202_ACCEPTED
)
async def trigger_sync(
    kb_id: str,
    tenant_id: Annotated[str, Depends(get_tenant_id)],
    _current_user: Annotated[CurrentUser, Depends(require_role(*_WRITE_ROLES))],
    store: Annotated[KnowledgeBaseStore, Depends(get_knowledge_base_store)],
    provisioner: Annotated[BedrockKnowledgeBaseProvisioner, Depends(get_bedrock_kb_provisioner)],
) -> SyncTriggerResponse:
    kb = await _get_kb_or_404(store, tenant_id, kb_id)
    _require_provisioned(kb)
    if kb.sync_status == "IN_PROGRESS":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "error": "sync_in_progress",
                "message": "A sync is already running for this knowledge base.",
            },
        )
    try:
        ingestion_job_id = await provisioner.start_sync(kb)
    except KnowledgeBaseProvisioningError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"error": "bedrock_unavailable", "message": str(exc)},
        ) from exc

    await store.update_sync_state(tenant_id, kb_id, sync_status="IN_PROGRESS")
    return SyncTriggerResponse(ingestion_job_id=ingestion_job_id, status="IN_PROGRESS")


@router.get("/{kb_id}/sync/status", response_model=SyncStatusResponse)
async def get_sync_status(
    kb_id: str,
    ingestion_job_id: Annotated[str, Query(...)],
    tenant_id: Annotated[str, Depends(get_tenant_id)],
    _current_user: Annotated[CurrentUser, Depends(require_role(*_READ_ROLES))],
    store: Annotated[KnowledgeBaseStore, Depends(get_knowledge_base_store)],
    provisioner: Annotated[BedrockKnowledgeBaseProvisioner, Depends(get_bedrock_kb_provisioner)],
) -> SyncStatusResponse:
    kb = await _get_kb_or_404(store, tenant_id, kb_id)
    try:
        result = await provisioner.get_sync_status(kb, ingestion_job_id)
    except KnowledgeBaseProvisioningError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"error": "bedrock_unavailable", "message": str(exc)},
        ) from exc

    if result["status"] in ("COMPLETE", "FAILED"):
        await store.update_sync_state(
            tenant_id,
            kb_id,
            sync_status=result["status"],
            sync_error=result.get("error"),
            mark_synced=result["status"] == "COMPLETE",
        )

    return SyncStatusResponse(**result)


# ── Presigned upload / bucket validation (Section 47, R59 corrected
# 2026-09-01) ────────────────────────────────────────────────────────────
# A KB is a standalone platform resource (see module docstring) — neither
# endpoint below is gated on any agent's deploy status. Files never touch
# this API's process: the browser PUTs directly to the customer's own S3
# bucket via a 15-minute presigned URL. Bulk: one request returns one URL
# per file so the browser can upload all of them in parallel.

_PRESIGNED_UPLOAD_TTL_SECONDS = 900  # 15 minutes


class PresignedUploadFile(BaseModel):
    filename: str
    content_type: str | None = None


class PresignedUploadRequest(BaseModel):
    files: list[PresignedUploadFile]
    subfolder: str | None = None


class PresignedUploadItem(BaseModel):
    filename: str
    s3_key: str
    upload_url: str


class PresignedUploadResponse(BaseModel):
    bucket: str
    uploads: list[PresignedUploadItem]
    expires_in_seconds: int = _PRESIGNED_UPLOAD_TTL_SECONDS


@router.post("/{kb_id}/presigned-upload", response_model=PresignedUploadResponse)
async def create_presigned_upload(
    kb_id: str,
    payload: PresignedUploadRequest,
    tenant_id: Annotated[str, Depends(get_tenant_id)],
    _current_user: Annotated[CurrentUser, Depends(require_role(*_WRITE_ROLES))],
    store: Annotated[KnowledgeBaseStore, Depends(get_knowledge_base_store)],
    s3_client: Annotated[Any, Depends(get_s3_client)],
) -> PresignedUploadResponse:
    kb = await _get_kb_or_404(store, tenant_id, kb_id)
    _require_provisioned(kb)
    assert kb.s3_bucket is not None and kb.s3_prefix is not None  # guarded by _require_provisioned

    for file in payload.files:
        ext = PurePosixPath(file.filename).suffix.lower()
        if ext not in _ALLOWED_DOCUMENT_EXTENSIONS:
            raise HTTPException(
                status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
                detail={
                    "error": "unsupported_file_type",
                    "message": (
                        f"File type {ext!r} is not supported. Allowed: "
                        + ", ".join(sorted(e.lstrip(".") for e in _ALLOWED_DOCUMENT_EXTENSIONS))
                    ),
                },
            )

    uploads: list[PresignedUploadItem] = []
    try:
        for file in payload.files:
            s3_key = _document_s3_key(kb, file.filename, payload.subfolder)
            params: dict[str, Any] = {"Bucket": kb.s3_bucket, "Key": s3_key}
            if file.content_type:
                params["ContentType"] = file.content_type
            upload_url = await asyncio.to_thread(
                s3_client.generate_presigned_url,
                ClientMethod="put_object",
                Params=params,
                ExpiresIn=_PRESIGNED_UPLOAD_TTL_SECONDS,
            )
            uploads.append(
                PresignedUploadItem(filename=file.filename, s3_key=s3_key, upload_url=upload_url)
            )
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "error": "storage_unavailable",
                "message": f"Could not create upload URL: {exc}",
            },
        ) from exc

    return PresignedUploadResponse(
        bucket=kb.s3_bucket,
        uploads=uploads,
        expires_in_seconds=_PRESIGNED_UPLOAD_TTL_SECONDS,
    )


class ValidateS3Request(BaseModel):
    bucket_name: str


class ValidateS3Response(BaseModel):
    accessible: bool
    bucket_name: str


@router.post("/{kb_id}/validate-s3", response_model=ValidateS3Response)
async def validate_s3_bucket(
    kb_id: str,
    payload: ValidateS3Request,
    tenant_id: Annotated[str, Depends(get_tenant_id)],
    _current_user: Annotated[CurrentUser, Depends(require_role(*_WRITE_ROLES))],
    store: Annotated[KnowledgeBaseStore, Depends(get_knowledge_base_store)],
    s3_client: Annotated[Any, Depends(get_s3_client)],
) -> ValidateS3Response:
    await _get_kb_or_404(store, tenant_id, kb_id)

    try:
        await asyncio.to_thread(s3_client.head_bucket, Bucket=payload.bucket_name)
    except ClientError as exc:
        error_code = str(exc.response.get("Error", {}).get("Code", "Unknown"))
        reason = {
            "404": "Bucket does not exist.",
            "403": "This Runtime does not have access to this bucket.",
        }.get(error_code, f"Bucket is not accessible ({error_code}).")
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"error": "bucket_not_accessible", "message": reason},
        ) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "error": "bucket_not_accessible",
                "message": f"Could not validate bucket: {exc}",
            },
        ) from exc

    return ValidateS3Response(accessible=True, bucket_name=payload.bucket_name)
