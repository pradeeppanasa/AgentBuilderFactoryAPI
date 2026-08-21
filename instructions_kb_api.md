# Knowledge Base (KB) — API Implementation Instructions
# API team only. UI does not touch S3 or Bedrock directly.

Authored: 2026-08-19
Drop this file at the root of the `panasa-agent-builder-runtime` repo.

---

## Architecture summary

```
UI (wizard Step 3)
      │
      │  multipart file upload / API calls only
      ▼
Panasa API (panasa-agent-builder-runtime)
      │
      ├── S3  →  raw document storage
      └── Bedrock Knowledge Bases  →  chunking, embedding, vector index (OpenSearch Serverless)
```

UI never calls S3 or Bedrock directly.
All KB operations go through the Panasa API.

---

## S3 folder structure

One bucket per environment. One prefix per KB.

```
s3://panasa-kb-documents-{account_id}/
  {tenant_id}/
    {kb_id}/
      raw/                      ← source files (what Bedrock syncs from)
        document1.pdf
        document2.docx
        subfolder/
          document3.txt
      metadata/
        sync_manifest.json      ← last sync timestamp + file count
```

### Rules
- All source files go under `{tenant_id}/{kb_id}/raw/`
- Sub-folders inside `raw/` are allowed — Bedrock crawls recursively
- Never write directly to the root prefix — always scope to `{tenant_id}/{kb_id}/`
- S3 key for every uploaded file: `{tenant_id}/{kb_id}/raw/{original_filename}`
  - If sub-folder provided: `{tenant_id}/{kb_id}/raw/{subfolder}/{filename}`
- File types supported by Bedrock KB: `.pdf`, `.docx`, `.doc`, `.txt`, `.md`, `.html`, `.csv`
- Reject unsupported file types at upload with HTTP 415

### New env var required
```dotenv
KB_DOCUMENTS_BUCKET=panasa-kb-documents-{account_id}
```

---

## DynamoDB — `panasa-knowledge-bases` table

**New table required.**

**Primary key:** `PK=tenant_id`, `SK=kb_id`

```python
class KnowledgeBaseRecord(BaseModel):
    tenant_id: str
    kb_id: str                      # e.g. "kb-kyc-001" — generated on create
    name: str
    description: str | None
    status: str                     # CREATING | ACTIVE | SYNCING | SYNC_FAILED | DELETING

    # S3
    s3_bucket: str                  # KB_DOCUMENTS_BUCKET value
    s3_prefix: str                  # "{tenant_id}/{kb_id}/raw/"

    # Bedrock
    bedrock_kb_id: str | None       # Bedrock-assigned ID (e.g. "ABCDEF1234") — set after create
    bedrock_ds_id: str | None       # Bedrock data source ID — set after create
    embedding_model: str            # "amazon.titan-embed-text-v2:0"
    chunk_strategy: str             # "semantic" | "fixed" | "paragraph"

    # Sync state
    last_synced_at: str | None      # ISO 8601
    document_count: int = 0
    sync_status: str | None         # "COMPLETE" | "FAILED" | "IN_PROGRESS"
    sync_error: str | None

    # Audit
    created_by: str
    created_at: str
    updated_at: str
```

---

## KBConfig model update (AgentConfiguration)

Update the existing `KBConfig` in `app/shared/models.py`:

```python
class KBConfig(BaseModel):
    enabled: bool = False
    kb_id: str | None = None          # ADD: references panasa-knowledge-bases table
    kb_name: str | None = None
    s3_bucket: str | None = None
    s3_prefix: str | None = None      # ADD: "{tenant_id}/{kb_id}/raw/"
    embedding_model: str = "amazon.titan-embed-text-v2:0"
    chunk_strategy: str = "semantic"  # "semantic" | "fixed" | "paragraph"
    top_k: int = 5
    reranking_enabled: bool = True
```

---

## API endpoints to implement

### KB CRUD

#### `POST /api/v1/knowledge-bases`
Create a new KB. Provisions the Bedrock KB and data source. Writes to DynamoDB.

**Request:**
```json
{
  "name": "KYC Policy Documents",
  "description": "Compliance and policy docs for KYC agent",
  "embedding_model": "amazon.titan-embed-text-v2:0",
  "chunk_strategy": "semantic"
}
```

**Implementation:**
```python
async def create_knowledge_base(request: CreateKBRequest, tenant_id: str):
    kb_id = f"kb-{slugify(request.name)}-{uuid4().hex[:6]}"
    s3_prefix = f"{tenant_id}/{kb_id}/raw/"

    # 1. Create Bedrock Knowledge Base
    bedrock = boto3.client("bedrock-agent")
    kb_response = bedrock.create_knowledge_base(
        name=f"panasa-{tenant_id}-{kb_id}",
        description=request.description or "",
        roleArn=settings.BEDROCK_KB_ROLE_ARN,
        knowledgeBaseConfiguration={
            "type": "VECTOR",
            "vectorKnowledgeBaseConfiguration": {
                "embeddingModelArn": f"arn:aws:bedrock:{settings.AWS_REGION}::foundation-model/{request.embedding_model}"
            }
        },
        storageConfiguration={
            "type": "OPENSEARCH_SERVERLESS",
            "opensearchServerlessConfiguration": {
                "collectionArn": settings.OPENSEARCH_COLLECTION_ARN,
                "vectorIndexName": f"panasa-{kb_id}-index",
                "fieldMapping": {
                    "vectorField": "embedding",
                    "textField": "text",
                    "metadataField": "metadata"
                }
            }
        }
    )
    bedrock_kb_id = kb_response["knowledgeBase"]["knowledgeBaseId"]

    # 2. Create Bedrock data source (points to S3 prefix)
    ds_response = bedrock.create_data_source(
        knowledgeBaseId=bedrock_kb_id,
        name=f"panasa-{kb_id}-s3-source",
        dataSourceConfiguration={
            "type": "S3",
            "s3Configuration": {
                "bucketArn": f"arn:aws:s3:::{settings.KB_DOCUMENTS_BUCKET}",
                "inclusionPrefixes": [s3_prefix]
            }
        },
        vectorIngestionConfiguration={
            "chunkingConfiguration": {
                "chunkingStrategy": request.chunk_strategy.upper()
            }
        }
    )
    bedrock_ds_id = ds_response["dataSource"]["dataSourceId"]

    # 3. Write to DynamoDB
    record = KnowledgeBaseRecord(
        tenant_id=tenant_id,
        kb_id=kb_id,
        name=request.name,
        description=request.description,
        status="ACTIVE",
        s3_bucket=settings.KB_DOCUMENTS_BUCKET,
        s3_prefix=s3_prefix,
        bedrock_kb_id=bedrock_kb_id,
        bedrock_ds_id=bedrock_ds_id,
        embedding_model=request.embedding_model,
        chunk_strategy=request.chunk_strategy,
        created_by=current_user.user_id,
        created_at=utcnow(),
        updated_at=utcnow(),
    )
    await db.put_knowledge_base(record)
    return record
```

**Response:** `201 Created` — full `KnowledgeBaseRecord`

---

#### `GET /api/v1/knowledge-bases`
List all KBs for the tenant.

**Response:** `200 OK` — list of `KnowledgeBaseRecord`

---

#### `GET /api/v1/knowledge-bases/{kb_id}`
Get a single KB record.

**Response:** `200 OK` — `KnowledgeBaseRecord`

---

#### `DELETE /api/v1/knowledge-bases/{kb_id}`
Delete KB. Removes Bedrock KB, data source, S3 prefix, and DynamoDB record.

**Implementation order:**
1. Delete Bedrock data source
2. Delete Bedrock knowledge base
3. Delete all S3 objects under `{tenant_id}/{kb_id}/`
4. Delete DynamoDB record

**Response:** `204 No Content`

---

### Document upload

#### `POST /api/v1/knowledge-bases/{kb_id}/documents`
Upload one or more files. Stores to S3. Does NOT auto-sync — caller must trigger sync separately.

**Request:** `multipart/form-data`
```
files: File[]          ← one or more files
subfolder: str | None  ← optional sub-folder within raw/
```

**Implementation:**
```python
async def upload_documents(kb_id: str, files: list[UploadFile], subfolder: str | None, tenant_id: str):
    kb = await db.get_knowledge_base(tenant_id, kb_id)
    s3 = boto3.client("s3")
    uploaded = []

    for file in files:
        # Validate file type
        ext = Path(file.filename).suffix.lower()
        if ext not in {".pdf", ".docx", ".doc", ".txt", ".md", ".html", ".csv"}:
            raise HTTPException(status_code=415, detail=f"Unsupported file type: {ext}")

        # Build S3 key
        if subfolder:
            s3_key = f"{kb.s3_prefix}{subfolder.strip('/')}/{file.filename}"
        else:
            s3_key = f"{kb.s3_prefix}{file.filename}"

        # Upload to S3
        content = await file.read()
        s3.put_object(
            Bucket=kb.s3_bucket,
            Key=s3_key,
            Body=content,
            ContentType=file.content_type,
            ServerSideEncryption="aws:kms",
        )
        uploaded.append({"filename": file.filename, "s3_key": s3_key, "size_bytes": len(content)})

    # Update document count
    await db.increment_kb_document_count(tenant_id, kb_id, len(uploaded))
    return {"uploaded": uploaded, "count": len(uploaded)}
```

**Response:** `200 OK`
```json
{
  "uploaded": [
    {"filename": "policy.pdf", "s3_key": "tenant-1/kb-001/raw/policy.pdf", "size_bytes": 204800}
  ],
  "count": 1
}
```

---

#### `GET /api/v1/knowledge-bases/{kb_id}/documents`
List all documents in the KB (read from S3 prefix, not DynamoDB).

**Implementation:** `s3.list_objects_v2(Bucket=..., Prefix=kb.s3_prefix)`

**Response:** `200 OK`
```json
{
  "documents": [
    {"filename": "policy.pdf", "s3_key": "...", "size_bytes": 204800, "last_modified": "2026-08-19T10:00:00Z"},
    {"filename": "regulations/gdpr.pdf", "s3_key": "...", "size_bytes": 512000, "last_modified": "2026-08-19T10:01:00Z"}
  ],
  "count": 2
}
```

---

#### `DELETE /api/v1/knowledge-bases/{kb_id}/documents/{s3_key}`
Delete a single document from S3. URL-encode the `s3_key`.

**Response:** `204 No Content`

---

### Sync

#### `POST /api/v1/knowledge-bases/{kb_id}/sync`
Trigger Bedrock ingestion job. Bedrock reads from the S3 `raw/` prefix and updates the vector index.

**Implementation:**
```python
async def trigger_sync(kb_id: str, tenant_id: str):
    kb = await db.get_knowledge_base(tenant_id, kb_id)

    bedrock = boto3.client("bedrock-agent")
    response = bedrock.start_ingestion_job(
        knowledgeBaseId=kb.bedrock_kb_id,
        dataSourceId=kb.bedrock_ds_id,
    )
    ingestion_job_id = response["ingestionJob"]["ingestionJobId"]

    # Update sync state in DynamoDB
    await db.update_knowledge_base(tenant_id, kb_id, {
        "sync_status": "IN_PROGRESS",
        "updated_at": utcnow(),
    })
    return {"ingestion_job_id": ingestion_job_id, "status": "IN_PROGRESS"}
```

**Response:** `202 Accepted`
```json
{"ingestion_job_id": "job-abc123", "status": "IN_PROGRESS"}
```

---

#### `GET /api/v1/knowledge-bases/{kb_id}/sync/status`
Poll sync progress. UI calls this every 3 seconds until status is `COMPLETE` or `FAILED`.

**Implementation:**
```python
async def get_sync_status(kb_id: str, ingestion_job_id: str, tenant_id: str):
    kb = await db.get_knowledge_base(tenant_id, kb_id)

    bedrock = boto3.client("bedrock-agent")
    response = bedrock.get_ingestion_job(
        knowledgeBaseId=kb.bedrock_kb_id,
        dataSourceId=kb.bedrock_ds_id,
        ingestionJobId=ingestion_job_id,
    )
    job = response["ingestionJob"]

    # Update DynamoDB on completion
    if job["status"] in ("COMPLETE", "FAILED"):
        await db.update_knowledge_base(tenant_id, kb_id, {
            "sync_status": job["status"],
            "last_synced_at": utcnow() if job["status"] == "COMPLETE" else None,
            "sync_error": job.get("failureReasons", [None])[0],
            "updated_at": utcnow(),
        })

    return {
        "status": job["status"],              # IN_PROGRESS | COMPLETE | FAILED
        "documents_indexed": job.get("statistics", {}).get("numberOfDocumentsIndexed", 0),
        "documents_failed": job.get("statistics", {}).get("numberOfDocumentsFailed", 0),
        "started_at": job.get("startedAt"),
        "updated_at": job.get("updatedAt"),
        "error": job.get("failureReasons", [None])[0],
    }
```

**Response:** `200 OK`
```json
{
  "status": "COMPLETE",
  "documents_indexed": 5,
  "documents_failed": 0,
  "started_at": "2026-08-19T10:05:00Z",
  "updated_at": "2026-08-19T10:05:42Z",
  "error": null
}
```

---

## New env vars required

```dotenv
KB_DOCUMENTS_BUCKET=panasa-kb-documents-{account_id}
BEDROCK_KB_ROLE_ARN=arn:aws:iam::{account_id}:role/panasa-bedrock-kb-role
OPENSEARCH_COLLECTION_ARN=arn:aws:aoss:{region}:{account_id}:collection/{collection_id}
```

---

## IAM — Bedrock KB role (IaC addition)

The `BEDROCK_KB_ROLE_ARN` role must allow:
```json
{
  "Effect": "Allow",
  "Action": [
    "s3:GetObject",
    "s3:ListBucket"
  ],
  "Resource": [
    "arn:aws:s3:::panasa-kb-documents-{account_id}",
    "arn:aws:s3:::panasa-kb-documents-{account_id}/*"
  ]
}
```
And:
```json
{
  "Effect": "Allow",
  "Action": [
    "aoss:APIAccessAll"
  ],
  "Resource": "arn:aws:aoss:{region}:{account_id}:collection/{collection_id}"
}
```

Add this IAM role to the Stage 1 Terraform bootstrap (not per-agent IaC — it is a platform-level role).

---

## Error handling

| Scenario | HTTP | Response body |
|---|---|---|
| Unsupported file type | 415 | `{"error": "unsupported_file_type", "message": "File type .xyz is not supported. Allowed: pdf, docx, txt, md, html, csv"}` |
| KB not found | 404 | `{"error": "kb_not_found"}` |
| Bedrock KB create fails | 503 | `{"error": "bedrock_unavailable", "message": "Could not create knowledge base. Check Bedrock permissions."}` |
| S3 upload fails | 503 | `{"error": "storage_unavailable", "message": "Could not upload file. Check S3 permissions."}` |
| Sync already in progress | 409 | `{"error": "sync_in_progress", "message": "A sync is already running for this knowledge base."}` |

Never return raw AWS exceptions or stack traces.

---

## UI — what to build (hand to UI team separately)

The API team does NOT build UI. Pass these requirements to the UI team:

- Wizard Step 3 → Knowledge Base section:
  - Dropdown: select existing KB or "Create new"
  - "Upload documents" button → file picker (multi-select) → calls `POST /documents`
  - After upload: "Sync now" button → calls `POST /sync`
  - Sync progress: poll `GET /sync/status` every 3 seconds → show progress bar
  - Document list: table showing filename, size, last modified
  - Delete document: trash icon → calls `DELETE /documents/{key}`

- Knowledge Base management page (separate from wizard):
  - List all KBs (`GET /knowledge-bases`)
  - Create KB form
  - Per-KB detail: document list + sync history + status

---

## Re-test checklist (for KB test stage)

- [ ] `POST /knowledge-bases` creates Bedrock KB + data source + DynamoDB record
- [ ] `POST /documents` uploads file to correct S3 prefix
- [ ] `POST /documents` rejects `.xlsx` with 415
- [ ] `POST /sync` triggers Bedrock ingestion job
- [ ] `GET /sync/status` returns COMPLETE after sync finishes
- [ ] `DELETE /documents/{key}` removes file from S3
- [ ] `DELETE /knowledge-bases/{id}` removes Bedrock KB + S3 prefix + DynamoDB record
- [ ] All error cases return structured JSON, no raw AWS errors
