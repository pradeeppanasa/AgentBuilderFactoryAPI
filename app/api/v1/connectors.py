"""Connector catalog (CLAUDE.md Section 5.4 / Phase 12)."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

from app.dependencies import get_connector_catalog_store, get_connector_tester, get_tenant_id
from app.modules.auth.dependencies import require_role
from app.modules.auth.schemas import CurrentUser
from app.modules.connectors.catalog import ConnectorCatalogStore
from app.modules.connectors.models import ConnectorRecord, ExecutorType
from app.modules.connectors.openapi_import import OpenAPIParseError, parse_openapi_document
from app.modules.connectors.tester import ConnectorTester, ConnectorTestResult

router = APIRouter(prefix="/connectors", tags=["connectors"])

_READ_ROLES = ("developer", "analyst", "auditor")
_WRITE_ROLES = ("developer",)


class ConnectorListResponse(BaseModel):
    items: list[ConnectorRecord]


class CreateConnectorRequest(BaseModel):
    name: str
    executor_type: ExecutorType
    description: str
    input_schema: dict[str, Any] = {}
    output_schema: dict[str, Any] = {}
    endpoint_template: str | None = None
    credentials_required: list[str] = []


class ConnectorTestRequest(BaseModel):
    endpoint_params: dict[str, str] = {}
    credentials: dict[str, str] = {}
    test_payload: dict[str, Any] | None = None


class ImportOpenAPIRequest(BaseModel):
    schema_document: dict[str, Any] | str
    """An OpenAPI 3.0 document — either parsed JSON or a raw JSON/YAML string
    (upload or paste, Section 3.6 Method B)."""


class ImportOpenAPIResponse(BaseModel):
    created: list[ConnectorRecord]


@router.get("", response_model=ConnectorListResponse)
async def list_connectors(
    tenant_id: Annotated[str, Depends(get_tenant_id)],
    _current_user: Annotated[CurrentUser, Depends(require_role(*_READ_ROLES))],
    store: Annotated[ConnectorCatalogStore, Depends(get_connector_catalog_store)],
) -> ConnectorListResponse:
    return ConnectorListResponse(items=await store.list_connectors(tenant_id))


@router.post("", response_model=ConnectorRecord, status_code=status.HTTP_201_CREATED)
async def create_connector(
    payload: CreateConnectorRequest,
    tenant_id: Annotated[str, Depends(get_tenant_id)],
    current_user: Annotated[CurrentUser, Depends(require_role(*_WRITE_ROLES))],
    store: Annotated[ConnectorCatalogStore, Depends(get_connector_catalog_store)],
) -> ConnectorRecord:
    return await store.create_connector(
        tenant_id=tenant_id,
        name=payload.name,
        executor_type=payload.executor_type,
        description=payload.description,
        created_by=current_user.email,
        input_schema=payload.input_schema,
        output_schema=payload.output_schema,
        endpoint_template=payload.endpoint_template,
        credentials_required=payload.credentials_required,
    )


@router.post(
    "/import-openapi", response_model=ImportOpenAPIResponse, status_code=status.HTTP_201_CREATED
)
async def import_openapi_connectors(
    payload: ImportOpenAPIRequest,
    tenant_id: Annotated[str, Depends(get_tenant_id)],
    current_user: Annotated[CurrentUser, Depends(require_role(*_WRITE_ROLES))],
    store: Annotated[ConnectorCatalogStore, Depends(get_connector_catalog_store)],
) -> ImportOpenAPIResponse:
    try:
        seeds = parse_openapi_document(payload.schema_document)
    except OpenAPIParseError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    created: list[ConnectorRecord] = []
    for seed in seeds:
        created.append(
            await store.create_connector(
                tenant_id=tenant_id,
                name=seed.name,
                executor_type="http",
                description=seed.description,
                created_by=current_user.email,
                input_schema=seed.input_schema,
                output_schema=seed.output_schema,
                endpoint_template=seed.endpoint_template,
                credentials_required=seed.credentials_required,
            )
        )
    return ImportOpenAPIResponse(created=created)


@router.get("/{connector_id}", response_model=ConnectorRecord)
async def get_connector(
    connector_id: str,
    tenant_id: Annotated[str, Depends(get_tenant_id)],
    _current_user: Annotated[CurrentUser, Depends(require_role(*_READ_ROLES))],
    store: Annotated[ConnectorCatalogStore, Depends(get_connector_catalog_store)],
) -> ConnectorRecord:
    record = await store.get_connector(tenant_id, connector_id)
    if record is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"Connector {connector_id!r} not found"
        )
    return record


@router.post("/{connector_id}/test", response_model=ConnectorTestResult)
async def test_connector(
    connector_id: str,
    payload: ConnectorTestRequest,
    tenant_id: Annotated[str, Depends(get_tenant_id)],
    _current_user: Annotated[CurrentUser, Depends(require_role(*_WRITE_ROLES))],
    store: Annotated[ConnectorCatalogStore, Depends(get_connector_catalog_store)],
    tester: Annotated[ConnectorTester, Depends(get_connector_tester)],
) -> ConnectorTestResult:
    connector = await store.get_connector(tenant_id, connector_id)
    if connector is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"Connector {connector_id!r} not found"
        )
    return await tester.test(
        connector,
        endpoint_params=payload.endpoint_params,
        credentials=payload.credentials,
        test_payload=payload.test_payload,
    )
