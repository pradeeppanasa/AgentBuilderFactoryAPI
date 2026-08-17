"""Human-in-the-Loop review queue (CLAUDE.md Section 38.7/38.8).

Creating a review is open to every authenticated role — it represents an
in-flight agent invocation surfacing a decision, not a human-initiated
action. Listing and deciding (approve/reject/request-info) are reviewer
actions, restricted the same way every other write endpoint in this API
restricts writes (developer role; admin implicitly allowed, see
`require_role`'s docstring).
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

from app.dependencies import get_hitl_review_store, get_registry_store, get_tenant_id
from app.modules.auth.dependencies import require_role
from app.modules.auth.schemas import CurrentUser
from app.modules.hitl.models import HitlReviewRecord, HitlReviewStatus
from app.modules.hitl.store import (
    HitlReviewAlreadyDecidedError,
    HitlReviewNotFoundError,
    HitlReviewStore,
)
from app.modules.registry.store import AgentRegistryStore

router = APIRouter(prefix="/hitl", tags=["hitl"])

_READ_ROLES = ("developer", "analyst", "auditor")
_WRITE_ROLES = ("developer",)

_DEFAULT_TIMEOUT_HOURS = 24


class HitlReviewListResponse(BaseModel):
    items: list[HitlReviewRecord]


class CreateHitlReviewRequest(BaseModel):
    agent_id: str
    trigger_condition: str
    context_summary: str


class HitlReviewDecisionRequest(BaseModel):
    decision_reason: str | None = None


@router.post("/reviews", response_model=HitlReviewRecord, status_code=status.HTTP_201_CREATED)
async def create_review(
    payload: CreateHitlReviewRequest,
    tenant_id: Annotated[str, Depends(get_tenant_id)],
    current_user: Annotated[CurrentUser, Depends(require_role(*_READ_ROLES))],
    store: Annotated[HitlReviewStore, Depends(get_hitl_review_store)],
    registry_store: Annotated[AgentRegistryStore, Depends(get_registry_store)],
) -> HitlReviewRecord:
    agent = await registry_store.get_agent(tenant_id, payload.agent_id)
    if agent is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Agent {payload.agent_id!r} not found",
        )

    timeout_hours = _DEFAULT_TIMEOUT_HOURS
    version = await registry_store.get_version(agent.agent_id, agent.current_version)
    if version is not None and version.configuration.hitl is not None:
        timeout_hours = version.configuration.hitl.timeout_hours

    return await store.create(
        tenant_id=tenant_id,
        agent_id=agent.agent_id,
        project_id=agent.project_id,
        trigger_condition=payload.trigger_condition,
        context_summary=payload.context_summary,
        timeout_hours=timeout_hours,
        requested_by=current_user.email,
    )


@router.get("/reviews", response_model=HitlReviewListResponse)
async def list_reviews(
    tenant_id: Annotated[str, Depends(get_tenant_id)],
    _current_user: Annotated[CurrentUser, Depends(require_role(*_READ_ROLES))],
    store: Annotated[HitlReviewStore, Depends(get_hitl_review_store)],
    review_status: HitlReviewStatus | None = None,
) -> HitlReviewListResponse:
    return HitlReviewListResponse(items=await store.list_reviews(tenant_id, review_status))


@router.get("/reviews/{review_id}", response_model=HitlReviewRecord)
async def get_review(
    review_id: str,
    tenant_id: Annotated[str, Depends(get_tenant_id)],
    _current_user: Annotated[CurrentUser, Depends(require_role(*_READ_ROLES))],
    store: Annotated[HitlReviewStore, Depends(get_hitl_review_store)],
) -> HitlReviewRecord:
    record = await store.get(tenant_id, review_id)
    if record is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"HITL review {review_id!r} not found"
        )
    return record


async def _decide(
    action: str,
    review_id: str,
    payload: HitlReviewDecisionRequest,
    tenant_id: str,
    current_user: CurrentUser,
    store: HitlReviewStore,
) -> HitlReviewRecord:
    method = {
        "approve": store.approve,
        "reject": store.reject,
        "request-info": store.request_info,
    }[action]
    try:
        return await method(tenant_id, review_id, current_user.email, payload.decision_reason)
    except HitlReviewNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except HitlReviewAlreadyDecidedError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc


@router.post("/reviews/{review_id}/approve", response_model=HitlReviewRecord)
async def approve_review(
    review_id: str,
    payload: HitlReviewDecisionRequest,
    tenant_id: Annotated[str, Depends(get_tenant_id)],
    current_user: Annotated[CurrentUser, Depends(require_role(*_WRITE_ROLES))],
    store: Annotated[HitlReviewStore, Depends(get_hitl_review_store)],
) -> HitlReviewRecord:
    return await _decide("approve", review_id, payload, tenant_id, current_user, store)


@router.post("/reviews/{review_id}/reject", response_model=HitlReviewRecord)
async def reject_review(
    review_id: str,
    payload: HitlReviewDecisionRequest,
    tenant_id: Annotated[str, Depends(get_tenant_id)],
    current_user: Annotated[CurrentUser, Depends(require_role(*_WRITE_ROLES))],
    store: Annotated[HitlReviewStore, Depends(get_hitl_review_store)],
) -> HitlReviewRecord:
    return await _decide("reject", review_id, payload, tenant_id, current_user, store)


@router.post("/reviews/{review_id}/request-info", response_model=HitlReviewRecord)
async def request_info_review(
    review_id: str,
    payload: HitlReviewDecisionRequest,
    tenant_id: Annotated[str, Depends(get_tenant_id)],
    current_user: Annotated[CurrentUser, Depends(require_role(*_WRITE_ROLES))],
    store: Annotated[HitlReviewStore, Depends(get_hitl_review_store)],
) -> HitlReviewRecord:
    return await _decide("request-info", review_id, payload, tenant_id, current_user, store)
