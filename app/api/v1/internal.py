"""Internal, machine-to-machine endpoints — never a JWT-authenticated user
request, never covered by app/modules/auth/dependencies.py's require_role().

POST /internal/deployment-complete (Generic Agent Runtime instruction,
2026-09-03, Part 6): the customer CI/CD's own final `terraform apply` step
calls this after a real deploy succeeds — the Runtime has no other way to
learn a real deployment finished. F2 describes the customer CI/CD writing
deployment status directly to DynamoDB, which presumes a Lambda/Step-
Functions pipeline (Section 6.2) this codebase doesn't actually run yet; a
plain `curl` from a generated GitHub Actions workflow (cicd_templates.py)
is what's real today, hence a webhook rather than a DynamoDB write. Mirrors
lambda_handlers/mark_active.py's exact logic (same two store calls, same
order) — that Lambda is the Step-Functions-pipeline's version of this same
job; this is the GitHub-Actions-pipeline's version.
"""

from __future__ import annotations

import hmac
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Header, HTTPException, status
from pydantic import BaseModel

from app.config import settings
from app.dependencies import get_deployment_status_store, get_registry_store
from app.modules.deployment.status_store import DeploymentNotFoundError, DeploymentStatusStore
from app.modules.registry.store import AgentRegistryStore
from app.shared.exceptions import AgentNotFoundError

router = APIRouter(prefix="/internal", tags=["internal"])


class DeploymentCompleteRequest(BaseModel):
    agent_id: str
    tenant_id: str
    deployment_id: str
    version: int
    status: Literal["ACTIVE"]
    """Only ACTIVE today — a real terraform apply failure is reported by
    the CI/CD job simply failing (visible in GitHub Actions/whichever
    provider itself); this endpoint isn't a general status-update channel,
    just the one "it worked" signal the Runtime can't observe on its own."""


class DeploymentCompleteResponse(BaseModel):
    agent_id: str
    status: str
    live_version: int


def _check_webhook_secret(authorization: str | None) -> None:
    configured_secret = settings.internal_webhook_secret
    if not configured_secret:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="INTERNAL_WEBHOOK_SECRET is not configured — this endpoint is disabled.",
        )
    provided = (authorization or "").removeprefix("Bearer ").strip()
    if not hmac.compare_digest(provided, configured_secret):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid webhook secret"
        )


@router.post("/deployment-complete", response_model=DeploymentCompleteResponse)
async def deployment_complete(
    payload: DeploymentCompleteRequest,
    registry_store: Annotated[AgentRegistryStore, Depends(get_registry_store)],
    deployment_status_store: Annotated[DeploymentStatusStore, Depends(get_deployment_status_store)],
    authorization: str | None = Header(default=None),
) -> DeploymentCompleteResponse:
    _check_webhook_secret(authorization)

    try:
        updated_agent = await registry_store.mark_deployment_active(
            tenant_id=payload.tenant_id,
            agent_id=payload.agent_id,
            live_version=payload.version,
            updated_by="ci-cd-webhook",
        )
    except AgentNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc

    try:
        await deployment_status_store.update_stage(
            agent_id=payload.agent_id,
            deployment_id=payload.deployment_id,
            stage="HEALTH_CHECK",
            stage_status="PASSED",
            overall_status="ACTIVE",
        )
    except DeploymentNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc

    return DeploymentCompleteResponse(
        agent_id=payload.agent_id,
        status="ACTIVE",
        live_version=updated_agent.live_version or payload.version,
    )
