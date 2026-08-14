"""Deployments — top-level status read (CLAUDE.md Section 5.3).

GET /deployments/{deployment_id} has no agent_id in its path, so the
deployment is resolved by the status store's deployment_id GSI first, then
checked against tenant ownership via the Agent Registry — the same
tenant-isolation pattern used everywhere else (R01), just applied after the
lookup instead of before it, since there's no other way to find the
deployment without already knowing the tenant/agent.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status

from app.dependencies import get_deployment_status_store, get_registry_store, get_tenant_id
from app.modules.auth.dependencies import require_role
from app.modules.auth.schemas import CurrentUser
from app.modules.deployment.models import DeploymentRecord
from app.modules.deployment.status_store import DeploymentStatusStore
from app.modules.registry.store import AgentRegistryStore

router = APIRouter(prefix="/deployments", tags=["deployments"])

_READ_ROLES = ("developer", "analyst", "auditor")


@router.get("/{deployment_id}", response_model=DeploymentRecord)
async def get_deployment(
    deployment_id: str,
    tenant_id: Annotated[str, Depends(get_tenant_id)],
    _current_user: Annotated[CurrentUser, Depends(require_role(*_READ_ROLES))],
    registry_store: Annotated[AgentRegistryStore, Depends(get_registry_store)],
    deployment_status_store: Annotated[DeploymentStatusStore, Depends(get_deployment_status_store)],
) -> DeploymentRecord:
    record = await deployment_status_store.get_deployment_by_id(deployment_id)
    if record is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Deployment {deployment_id!r} not found",
        )

    if await registry_store.get_agent(tenant_id, record.agent_id) is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Deployment {deployment_id!r} not found",
        )

    return record
