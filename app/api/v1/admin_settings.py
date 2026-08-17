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

from datetime import UTC, datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from app.dependencies import get_platform_settings_store, get_secrets_manager, get_tenant_id
from app.modules.auth.dependencies import require_role
from app.modules.auth.schemas import CurrentUser
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


class ObservabilityConfigResponse(BaseModel):
    default_stack: DefaultStackStatus
    otel: OtelEndpointConfig
    langfuse: LangfuseConfig
    datadog: DatadogConfig


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
    )


@router.get("/observability", response_model=ObservabilityConfigResponse)
async def get_observability_config(
    tenant_id: Annotated[str, Depends(get_tenant_id)],
    current_user: Annotated[CurrentUser, Depends(require_role())],
    store: Annotated[PlatformSettingsStore, Depends(get_platform_settings_store)],
) -> ObservabilityConfigResponse:
    return await _load_response(tenant_id, store, current_user.email)


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
