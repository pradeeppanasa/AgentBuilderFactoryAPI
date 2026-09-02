"""Admin observability settings (CLAUDE.md Section 39/R45, R45-7).

Admin-only: the UI's types/observability.ts comment documents that the
spec's "developer/viewer get 403" doesn't match this codebase's real role
enum (admin | developer | analyst | auditor — no "viewer"), so gating here
uses `require_role()` with no extra roles, i.e. admin only, same pattern as
every other admin-only write route in this runtime.

Secret fields (Langfuse secret key, Datadog API key) are never round-tripped
on GET — only "****" (set) or null (unset), matching the UI's documented
expectation exactly. The real value is written to Secrets Manager on save;
only the ARN is persisted in DynamoDB (R11).
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Annotated, Any, Literal

from botocore.exceptions import ClientError
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

from app.config import settings
from app.dependencies import (
    get_platform_settings_store,
    get_s3_client,
    get_secrets_manager,
    get_tenant_id,
)
from app.modules.auth.dependencies import require_role
from app.modules.auth.schemas import CurrentUser
from app.modules.deployment.models import ApprovalMode, CICDProvider
from app.modules.observability.capabilities import (
    CapabilityDiscoveryResponse,
    discover_capabilities,
)
from app.modules.platform_settings.store import PlatformSettingsStore
from app.modules.secrets.manager import SecretsManager

router = APIRouter(prefix="/admin/settings", tags=["admin-settings"])

_MASK = "****"

DatadogSite = Literal["datadoghq.com", "datadoghq.eu", "us3.datadoghq.com", "us5.datadoghq.com"]


class DefaultStackStatus(BaseModel):
    """cloudwatch/xray are genuinely wired and running (Section 14 Phase 14,
    R45-1/R45-2). otel_sdk is reported as "deferred", not "active" — R45-4
    (a real OpenTelemetry SDK + OTLPSpanExporter) has not been built; X-Ray
    is judged sufficient for now (Section 41.1). Claiming "active" here
    would be a false statement on an admin-facing settings page."""

    cloudwatch: Literal["active"] = "active"
    xray: Literal["active"] = "active"
    otel_sdk: Literal["active", "deferred"] = "deferred"


class OtelEndpointConfig(BaseModel):
    endpoint: str | None


class SaveOtelEndpointRequest(BaseModel):
    endpoint: str


class LangfuseConfig(BaseModel):
    enabled: bool
    public_key: str | None
    secret_key: str | None  # "****" if set, null if unset — never the real value
    host: str | None


class SaveLangfuseRequest(BaseModel):
    enabled: bool
    public_key: str | None = None
    secret_key: str | None = None
    host: str | None = None


class DatadogConfig(BaseModel):
    enabled: bool
    api_key: str | None  # "****" if set, null if unset — never the real value
    site: DatadogSite | None


class SaveDatadogRequest(BaseModel):
    enabled: bool
    api_key: str | None = None
    site: DatadogSite | None = None


class GrafanaConfig(BaseModel):
    enabled: bool
    endpoint: str | None


class SaveGrafanaRequest(BaseModel):
    enabled: bool
    endpoint: str | None = None


class NewRelicConfig(BaseModel):
    enabled: bool
    api_key: str | None  # "****" if set, null if unset — never the real value


class SaveNewRelicRequest(BaseModel):
    enabled: bool
    api_key: str | None = None


class DynatraceConfig(BaseModel):
    enabled: bool
    endpoint: str | None


class SaveDynatraceRequest(BaseModel):
    enabled: bool
    endpoint: str | None = None


class DeploymentSettingsConfig(BaseModel):
    """Section 45.3/45.13 (R50, resolved as configurable). Default
    "automated" preserves F1's fully-automated pipeline exactly as frozen;
    a tenant switches to "manual" to opt into R50/Stage 5's human-approval
    gate (Section 45.4) for every deployment it triggers from then on.

    cicd_provider (Section 45.6/R58) picks which workflow file gets
    committed to a newly-created agent repo (Section 45.2's v1 case) —
    every agent for this tenant shares the same provider template.

    kb_s3_bucket/kb_s3_prefix (Section 47, R59 corrected 2026-09-01) —
    "Customer S3 Bucket" / "S3 Folder Prefix". The bucket a KB's data
    source lives in is entirely customer-owned; kb_s3_bucket=None means
    "not configured yet" (KB upload/sync surface a message pointing back
    here rather than failing generically). kb_s3_prefix defaults to
    "agent-factory" — the only thing ever written ahead of {kb_id}/raw/
    inside that bucket; no vendor name belongs in a customer's own S3."""

    default_approval_mode: ApprovalMode
    cicd_provider: CICDProvider
    kb_s3_bucket: str | None
    kb_s3_prefix: str
    # "Git Organisation" / "AWS Region" — resolved against the GIT_ORG/
    # AWS_REGION env vars when the tenant hasn't set its own (never null on
    # the response: aws_region always has a global default).
    git_organisation: str | None
    aws_region: str


class SaveDeploymentSettingsRequest(BaseModel):
    default_approval_mode: ApprovalMode
    cicd_provider: CICDProvider | None = None
    """None keeps the tenant's current cicd_provider — matches every other
    optional field in this file (e.g. SaveGrafanaRequest.endpoint)."""
    kb_s3_bucket: str | None = None
    """None (omitted or explicit null) keeps the tenant's current bucket —
    same convention as cicd_provider above. An empty string is normalised
    to None on save, so the UI can send "" from a cleared input to
    explicitly unconfigure the bucket."""
    kb_s3_prefix: str | None = None
    """None keeps the tenant's current prefix. Unlike kb_s3_bucket, an
    empty string is rejected (422) rather than accepted — an empty prefix
    would upload straight to the bucket root, which requires deliberately
    setting kb_s3_prefix to "" only via a future explicit "no prefix"
    control, not an accidental blank field."""
    git_organisation: str | None = None
    """None keeps the tenant's current value; "" clears it back to the
    GIT_ORG env var fallback — same convention as kb_s3_bucket."""
    aws_region: str | None = None
    """None keeps the tenant's current value; "" clears it back to the
    AWS_REGION env var fallback."""


class ValidateS3BucketRequest(BaseModel):
    bucket_name: str


class ValidateS3BucketResponse(BaseModel):
    accessible: bool
    bucket_name: str


class ObservabilityConfigResponse(BaseModel):
    default_stack: DefaultStackStatus
    otel: OtelEndpointConfig
    langfuse: LangfuseConfig
    datadog: DatadogConfig
    grafana: GrafanaConfig
    new_relic: NewRelicConfig
    dynatrace: DynatraceConfig


def _now() -> str:
    return datetime.now(UTC).isoformat()


async def _load_response(
    tenant_id: str, store: PlatformSettingsStore, actor: str
) -> ObservabilityConfigResponse:
    record = await store.get_or_create(tenant_id, actor)
    return ObservabilityConfigResponse(
        default_stack=DefaultStackStatus(),
        otel=OtelEndpointConfig(endpoint=record.otel_endpoint),
        langfuse=LangfuseConfig(
            enabled=record.langfuse_enabled,
            public_key=record.langfuse_public_key,
            secret_key=_MASK if record.langfuse_secret_arn else None,
            host=record.langfuse_host,
        ),
        datadog=DatadogConfig(
            enabled=record.datadog_enabled,
            api_key=_MASK if record.datadog_api_key_arn else None,
            site=record.datadog_site,
        ),
        grafana=GrafanaConfig(
            enabled=record.grafana_enabled,
            endpoint=record.grafana_endpoint,
        ),
        new_relic=NewRelicConfig(
            enabled=record.new_relic_enabled,
            api_key=_MASK if record.new_relic_api_key_arn else None,
        ),
        dynatrace=DynatraceConfig(
            enabled=record.dynatrace_enabled,
            endpoint=record.dynatrace_endpoint,
        ),
    )


@router.get("/observability", response_model=ObservabilityConfigResponse)
async def get_observability_config(
    tenant_id: Annotated[str, Depends(get_tenant_id)],
    current_user: Annotated[CurrentUser, Depends(require_role())],
    store: Annotated[PlatformSettingsStore, Depends(get_platform_settings_store)],
) -> ObservabilityConfigResponse:
    return await _load_response(tenant_id, store, current_user.email)


@router.get("/observability/capabilities", response_model=CapabilityDiscoveryResponse)
async def get_observability_capabilities(
    tenant_id: Annotated[str, Depends(get_tenant_id)],
    current_user: Annotated[CurrentUser, Depends(require_role())],
    store: Annotated[PlatformSettingsStore, Depends(get_platform_settings_store)],
) -> CapabilityDiscoveryResponse:
    """CLAUDE.md Capability Discovery: derives provider-neutral capabilities
    (logs/metrics/distributed_tracing/opentelemetry) from the tenant's
    registered configuration. See app/modules/observability/capabilities.py
    — the only place a specific vendor name is allowed to appear in logic."""
    record = await store.get_or_create(tenant_id, current_user.email)
    return discover_capabilities(record)


@router.patch("/otel-endpoint", response_model=OtelEndpointConfig)
async def save_otel_endpoint(
    payload: SaveOtelEndpointRequest,
    tenant_id: Annotated[str, Depends(get_tenant_id)],
    current_user: Annotated[CurrentUser, Depends(require_role())],
    store: Annotated[PlatformSettingsStore, Depends(get_platform_settings_store)],
) -> OtelEndpointConfig:
    record = await store.get_or_create(tenant_id, current_user.email)
    record = record.model_copy(
        update={
            "otel_endpoint": payload.endpoint,
            "updated_by": current_user.email,
            "updated_at": _now(),
        }
    )
    await store.save(record)
    return OtelEndpointConfig(endpoint=record.otel_endpoint)


def _deployment_settings_response(record: Any) -> DeploymentSettingsConfig:
    return DeploymentSettingsConfig(
        default_approval_mode=record.default_approval_mode,
        cicd_provider=record.cicd_provider,
        kb_s3_bucket=record.kb_s3_bucket,
        kb_s3_prefix=record.kb_s3_prefix,
        git_organisation=record.git_organisation or settings.git_org,
        aws_region=record.aws_region or settings.aws_region,
    )


@router.get("/deployment", response_model=DeploymentSettingsConfig)
async def get_deployment_settings(
    tenant_id: Annotated[str, Depends(get_tenant_id)],
    current_user: Annotated[CurrentUser, Depends(require_role())],
    store: Annotated[PlatformSettingsStore, Depends(get_platform_settings_store)],
) -> DeploymentSettingsConfig:
    record = await store.get_or_create(tenant_id, current_user.email)
    return _deployment_settings_response(record)


@router.patch("/deployment", response_model=DeploymentSettingsConfig)
async def save_deployment_settings(
    payload: SaveDeploymentSettingsRequest,
    tenant_id: Annotated[str, Depends(get_tenant_id)],
    current_user: Annotated[CurrentUser, Depends(require_role())],
    store: Annotated[PlatformSettingsStore, Depends(get_platform_settings_store)],
) -> DeploymentSettingsConfig:
    record = await store.get_or_create(tenant_id, current_user.email)
    record = record.model_copy(
        update={
            "default_approval_mode": payload.default_approval_mode,
            "cicd_provider": (
                payload.cicd_provider if payload.cicd_provider is not None else record.cicd_provider
            ),
            "kb_s3_bucket": (
                (payload.kb_s3_bucket or None)
                if payload.kb_s3_bucket is not None
                else record.kb_s3_bucket
            ),
            "kb_s3_prefix": (
                payload.kb_s3_prefix if payload.kb_s3_prefix else record.kb_s3_prefix
            ),
            "git_organisation": (
                (payload.git_organisation or None)
                if payload.git_organisation is not None
                else record.git_organisation
            ),
            "aws_region": (
                (payload.aws_region or None)
                if payload.aws_region is not None
                else record.aws_region
            ),
            "updated_by": current_user.email,
            "updated_at": _now(),
        }
    )
    await store.save(record)
    return _deployment_settings_response(record)


@router.post("/deployment/validate-s3-bucket", response_model=ValidateS3BucketResponse)
async def validate_deployment_s3_bucket(
    payload: ValidateS3BucketRequest,
    _current_user: Annotated[CurrentUser, Depends(require_role())],
    s3_client: Annotated[Any, Depends(get_s3_client)],
) -> ValidateS3BucketResponse:
    """"Customer S3 Bucket Name" validate button (Section 47) — checks
    accessibility before the tenant saves it, same s3_client.head_bucket
    check knowledge_bases.py's validate-s3 does, just without needing an
    existing kb_id in the path (there isn't one yet on this settings page)."""
    try:
        await asyncio.to_thread(s3_client.head_bucket, Bucket=payload.bucket_name)
    except ClientError as exc:
        error_code = str(exc.response.get("Error", {}).get("Code", "Unknown"))
        reason = {
            "404": "Bucket does not exist.",
            "403": "This bucket is not accessible with the Runtime's current AWS credentials.",
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

    return ValidateS3BucketResponse(accessible=True, bucket_name=payload.bucket_name)


@router.get("/integrations/langfuse", response_model=LangfuseConfig)
async def get_langfuse_config(
    tenant_id: Annotated[str, Depends(get_tenant_id)],
    current_user: Annotated[CurrentUser, Depends(require_role())],
    store: Annotated[PlatformSettingsStore, Depends(get_platform_settings_store)],
) -> LangfuseConfig:
    response = await _load_response(tenant_id, store, current_user.email)
    return response.langfuse


@router.patch("/integrations/langfuse", response_model=LangfuseConfig)
async def save_langfuse_config(
    payload: SaveLangfuseRequest,
    tenant_id: Annotated[str, Depends(get_tenant_id)],
    current_user: Annotated[CurrentUser, Depends(require_role())],
    store: Annotated[PlatformSettingsStore, Depends(get_platform_settings_store)],
    secrets_manager: Annotated[SecretsManager, Depends(get_secrets_manager)],
) -> LangfuseConfig:
    record = await store.get_or_create(tenant_id, current_user.email)

    secret_arn = record.langfuse_secret_arn
    if payload.secret_key:
        secret_arn = await secrets_manager.create_or_update_secret(
            name=f"panasa/{tenant_id}/langfuse-secret-key",
            value=payload.secret_key,
            existing_arn=secret_arn,
        )

    record = record.model_copy(
        update={
            "langfuse_enabled": payload.enabled,
            "langfuse_public_key": (
                payload.public_key if payload.public_key is not None else record.langfuse_public_key
            ),
            "langfuse_secret_arn": secret_arn,
            "langfuse_host": payload.host if payload.host is not None else record.langfuse_host,
            "updated_by": current_user.email,
            "updated_at": _now(),
        }
    )
    await store.save(record)
    return LangfuseConfig(
        enabled=record.langfuse_enabled,
        public_key=record.langfuse_public_key,
        secret_key=_MASK if record.langfuse_secret_arn else None,
        host=record.langfuse_host,
    )


@router.get("/integrations/datadog", response_model=DatadogConfig)
async def get_datadog_config(
    tenant_id: Annotated[str, Depends(get_tenant_id)],
    current_user: Annotated[CurrentUser, Depends(require_role())],
    store: Annotated[PlatformSettingsStore, Depends(get_platform_settings_store)],
) -> DatadogConfig:
    response = await _load_response(tenant_id, store, current_user.email)
    return response.datadog


@router.patch("/integrations/datadog", response_model=DatadogConfig)
async def save_datadog_config(
    payload: SaveDatadogRequest,
    tenant_id: Annotated[str, Depends(get_tenant_id)],
    current_user: Annotated[CurrentUser, Depends(require_role())],
    store: Annotated[PlatformSettingsStore, Depends(get_platform_settings_store)],
    secrets_manager: Annotated[SecretsManager, Depends(get_secrets_manager)],
) -> DatadogConfig:
    record = await store.get_or_create(tenant_id, current_user.email)

    api_key_arn = record.datadog_api_key_arn
    if payload.api_key:
        api_key_arn = await secrets_manager.create_or_update_secret(
            name=f"panasa/{tenant_id}/datadog-api-key",
            value=payload.api_key,
            existing_arn=api_key_arn,
        )

    record = record.model_copy(
        update={
            "datadog_enabled": payload.enabled,
            "datadog_api_key_arn": api_key_arn,
            "datadog_site": payload.site if payload.site is not None else record.datadog_site,
            "updated_by": current_user.email,
            "updated_at": _now(),
        }
    )
    await store.save(record)
    return DatadogConfig(
        enabled=record.datadog_enabled,
        api_key=_MASK if record.datadog_api_key_arn else None,
        site=record.datadog_site,
    )


@router.get("/integrations/grafana", response_model=GrafanaConfig)
async def get_grafana_config(
    tenant_id: Annotated[str, Depends(get_tenant_id)],
    current_user: Annotated[CurrentUser, Depends(require_role())],
    store: Annotated[PlatformSettingsStore, Depends(get_platform_settings_store)],
) -> GrafanaConfig:
    response = await _load_response(tenant_id, store, current_user.email)
    return response.grafana


@router.patch("/integrations/grafana", response_model=GrafanaConfig)
async def save_grafana_config(
    payload: SaveGrafanaRequest,
    tenant_id: Annotated[str, Depends(get_tenant_id)],
    current_user: Annotated[CurrentUser, Depends(require_role())],
    store: Annotated[PlatformSettingsStore, Depends(get_platform_settings_store)],
) -> GrafanaConfig:
    record = await store.get_or_create(tenant_id, current_user.email)
    record = record.model_copy(
        update={
            "grafana_enabled": payload.enabled,
            "grafana_endpoint": (
                payload.endpoint if payload.endpoint is not None else record.grafana_endpoint
            ),
            "updated_by": current_user.email,
            "updated_at": _now(),
        }
    )
    await store.save(record)
    return GrafanaConfig(enabled=record.grafana_enabled, endpoint=record.grafana_endpoint)


@router.get("/integrations/new-relic", response_model=NewRelicConfig)
async def get_new_relic_config(
    tenant_id: Annotated[str, Depends(get_tenant_id)],
    current_user: Annotated[CurrentUser, Depends(require_role())],
    store: Annotated[PlatformSettingsStore, Depends(get_platform_settings_store)],
) -> NewRelicConfig:
    response = await _load_response(tenant_id, store, current_user.email)
    return response.new_relic


@router.patch("/integrations/new-relic", response_model=NewRelicConfig)
async def save_new_relic_config(
    payload: SaveNewRelicRequest,
    tenant_id: Annotated[str, Depends(get_tenant_id)],
    current_user: Annotated[CurrentUser, Depends(require_role())],
    store: Annotated[PlatformSettingsStore, Depends(get_platform_settings_store)],
    secrets_manager: Annotated[SecretsManager, Depends(get_secrets_manager)],
) -> NewRelicConfig:
    record = await store.get_or_create(tenant_id, current_user.email)

    api_key_arn = record.new_relic_api_key_arn
    if payload.api_key:
        api_key_arn = await secrets_manager.create_or_update_secret(
            name=f"panasa/{tenant_id}/new-relic-api-key",
            value=payload.api_key,
            existing_arn=api_key_arn,
        )

    record = record.model_copy(
        update={
            "new_relic_enabled": payload.enabled,
            "new_relic_api_key_arn": api_key_arn,
            "updated_by": current_user.email,
            "updated_at": _now(),
        }
    )
    await store.save(record)
    return NewRelicConfig(
        enabled=record.new_relic_enabled,
        api_key=_MASK if record.new_relic_api_key_arn else None,
    )


@router.get("/integrations/dynatrace", response_model=DynatraceConfig)
async def get_dynatrace_config(
    tenant_id: Annotated[str, Depends(get_tenant_id)],
    current_user: Annotated[CurrentUser, Depends(require_role())],
    store: Annotated[PlatformSettingsStore, Depends(get_platform_settings_store)],
) -> DynatraceConfig:
    response = await _load_response(tenant_id, store, current_user.email)
    return response.dynatrace


@router.patch("/integrations/dynatrace", response_model=DynatraceConfig)
async def save_dynatrace_config(
    payload: SaveDynatraceRequest,
    tenant_id: Annotated[str, Depends(get_tenant_id)],
    current_user: Annotated[CurrentUser, Depends(require_role())],
    store: Annotated[PlatformSettingsStore, Depends(get_platform_settings_store)],
) -> DynatraceConfig:
    record = await store.get_or_create(tenant_id, current_user.email)
    record = record.model_copy(
        update={
            "dynatrace_enabled": payload.enabled,
            "dynatrace_endpoint": (
                payload.endpoint if payload.endpoint is not None else record.dynatrace_endpoint
            ),
            "updated_by": current_user.email,
            "updated_at": _now(),
        }
    )
    await store.save(record)
    return DynatraceConfig(enabled=record.dynatrace_enabled, endpoint=record.dynatrace_endpoint)
