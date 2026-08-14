import uuid
from datetime import UTC, datetime
from typing import Annotated, Any

import redis.asyncio as redis
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

from app.config import settings
from app.dependencies import (
    get_dynamodb_resource,
    get_platform_upgrade_orchestrator,
    get_platform_upgrade_store,
    get_platform_version_service,
    get_redis_client,
    get_s3_client,
)
from app.modules.auth.dependencies import require_role
from app.modules.auth.schemas import CurrentUser
from app.modules.models.catalog import ModelInfo, get_model_catalog
from app.modules.platform.health import (
    check_cache,
    check_database,
    check_model_router,
    check_observability,
    check_storage,
)
from app.modules.platform.upgrade_models import PlatformUpgradeRecord, initial_upgrade_stages
from app.modules.platform.upgrade_orchestrator import (
    PlatformUpgradeNotConfiguredError,
    PlatformUpgradeOrchestrator,
)
from app.modules.platform.upgrade_store import PlatformUpgradeStatusStore
from app.modules.platform.version_service import PlatformVersionInfo, PlatformVersionService

router = APIRouter(prefix="/platform", tags=["platform"])


class ServiceHealth(BaseModel):
    database: str
    cache: str
    storage: str
    model_router: str
    observability: str


class HealthResponse(BaseModel):
    status: str
    version: str
    mode: str
    services: ServiceHealth


class ModelCatalogResponse(BaseModel):
    models: list[ModelInfo]


@router.get("/health", response_model=HealthResponse)
async def health(
    dynamodb_resource: Annotated[Any, Depends(get_dynamodb_resource)],
    s3_client: Annotated[Any, Depends(get_s3_client)],
    redis_client: Annotated[redis.Redis, Depends(get_redis_client)],
) -> HealthResponse:
    services = ServiceHealth(
        database=await check_database(dynamodb_resource, settings),
        cache=await check_cache(redis_client),
        storage=await check_storage(s3_client, settings),
        model_router=check_model_router(),
        observability=await check_observability(settings),
    )
    return HealthResponse(
        status="ok",
        version=settings.platform_version,
        mode=settings.deployment_mode,
        services=services,
    )


@router.get("/models", response_model=ModelCatalogResponse)
async def list_models() -> ModelCatalogResponse:
    return ModelCatalogResponse(models=get_model_catalog())


# ── Platform upgrade (Phase 15) ─────────────────────────────────────────
# Admin-only (require_role() with no extra roles — see app.modules.auth.
# dependencies.require_role's docstring: "admin" is always implicitly
# allowed, so an empty role set means admin-only). Unlike the agent
# deployment pipeline, this is a factory-wide operation with no tenant_id.


class UpgradeRequest(BaseModel):
    target_version: str | None = None
    """Omit to upgrade to the highest version currently available in ECR."""


class UpgradeResponse(BaseModel):
    upgrade_id: str
    status: str
    from_version: str
    target_version: str
    execution_arn: str


@router.get("/version", response_model=PlatformVersionInfo)
async def get_version(
    version_service: Annotated[PlatformVersionService, Depends(get_platform_version_service)],
) -> PlatformVersionInfo:
    return await version_service.get_version_info()


@router.post("/upgrade", response_model=UpgradeResponse, status_code=status.HTTP_202_ACCEPTED)
async def trigger_upgrade(
    payload: UpgradeRequest,
    current_user: Annotated[CurrentUser, Depends(require_role())],
    version_service: Annotated[PlatformVersionService, Depends(get_platform_version_service)],
    upgrade_store: Annotated[PlatformUpgradeStatusStore, Depends(get_platform_upgrade_store)],
    upgrade_orchestrator: Annotated[
        PlatformUpgradeOrchestrator, Depends(get_platform_upgrade_orchestrator)
    ],
) -> UpgradeResponse:
    target_version = payload.target_version
    if target_version is None:
        version_info = await version_service.get_version_info()
        target_version = version_info.available_update
        if target_version is None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="No target_version given and no newer version is available in ECR",
            )

    if not settings.runtime_image:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="RUNTIME_IMAGE is not configured",
        )
    target_image = f"{settings.runtime_image.rsplit(':', 1)[0]}:{target_version}"

    upgrade_id = f"UPG-{uuid.uuid4().hex[:8].upper()}"
    now = datetime.now(UTC).isoformat()
    await upgrade_store.create_upgrade(
        PlatformUpgradeRecord(
            upgrade_id=upgrade_id,
            from_version=settings.platform_version,
            target_version=target_version,
            target_image=target_image,
            stages=initial_upgrade_stages(),
            triggered_by=current_user.email,
            triggered_at=now,
            updated_at=now,
        )
    )

    try:
        execution_arn = await upgrade_orchestrator.start_upgrade(
            upgrade_id=upgrade_id,
            from_version=settings.platform_version,
            target_version=target_version,
            target_image=target_image,
        )
    except PlatformUpgradeNotConfiguredError as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(exc)
        ) from exc

    return UpgradeResponse(
        upgrade_id=upgrade_id,
        status="PENDING",
        from_version=settings.platform_version,
        target_version=target_version,
        execution_arn=execution_arn,
    )


@router.get("/upgrades/{upgrade_id}", response_model=PlatformUpgradeRecord)
async def get_upgrade(
    upgrade_id: str,
    _current_user: Annotated[CurrentUser, Depends(require_role())],
    upgrade_store: Annotated[PlatformUpgradeStatusStore, Depends(get_platform_upgrade_store)],
) -> PlatformUpgradeRecord:
    record = await upgrade_store.get_upgrade(upgrade_id)
    if record is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Platform upgrade {upgrade_id!r} not found",
        )
    return record
