"""Agent Runs (Observability — Runs Feature, Phase 1).

A Run is one execution of the *Generated Agent Runtime* serving real
business traffic. This Builder Runtime doesn't run that service (F8), so in
this Stage 1 environment — where no agent has ever gone through a real
`terraform apply` (R46) — there is no real run telemetry anywhere to show.
`POST /agents/{id}/runs/seed-demo` exists solely so the Runs list/detail/
filters can be exercised end to end without that missing piece; it is
gated behind settings.seed_runs_enabled (default False, same pattern as
mock_llm/mock_bedrock_guardrails) and never runs in prototype/enterprise.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

from app.config import settings
from app.dependencies import get_registry_store, get_run_store, get_tenant_id
from app.modules.auth.dependencies import require_role
from app.modules.auth.schemas import CurrentUser
from app.modules.registry.store import AgentRegistryStore
from app.modules.runs.models import LogListResponse, RunRecord, RunStatus, RunSummary, RunTrigger
from app.modules.runs.store import RunStore

router = APIRouter(prefix="/agents", tags=["runs"])

_READ_ROLES = ("developer", "analyst", "auditor")
_WRITE_ROLES = ("developer",)


class RunListResponse(BaseModel):
    items: list[RunRecord]


async def _require_agent(
    tenant_id: str, agent_id: str, store: AgentRegistryStore
) -> None:
    agent = await store.get_agent(tenant_id, agent_id)
    if agent is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"Agent {agent_id!r} not found"
        )


@router.get("/{agent_id}/runs", response_model=RunListResponse)
async def list_runs(
    agent_id: str,
    tenant_id: Annotated[str, Depends(get_tenant_id)],
    _current_user: Annotated[CurrentUser, Depends(require_role(*_READ_ROLES))],
    registry_store: Annotated[AgentRegistryStore, Depends(get_registry_store)],
    run_store: Annotated[RunStore, Depends(get_run_store)],
    status_filter: RunStatus | None = None,
    trigger: RunTrigger | None = None,
    version: int | None = None,
    limit: int = 50,
) -> RunListResponse:
    await _require_agent(tenant_id, agent_id, registry_store)
    items = await run_store.list_runs(
        tenant_id=tenant_id,
        agent_id=agent_id,
        status=status_filter,
        trigger=trigger,
        version=version,
        limit=limit,
    )
    return RunListResponse(items=items)


@router.get("/{agent_id}/runs/summary", response_model=RunSummary)
async def get_runs_summary(
    agent_id: str,
    tenant_id: Annotated[str, Depends(get_tenant_id)],
    _current_user: Annotated[CurrentUser, Depends(require_role(*_READ_ROLES))],
    registry_store: Annotated[AgentRegistryStore, Depends(get_registry_store)],
    run_store: Annotated[RunStore, Depends(get_run_store)],
    window_days: int = 7,
) -> RunSummary:
    """Section 10 — agent-level analytics header. Registered before
    /{agent_id}/runs/{run_id} so "summary" is never swallowed as a run_id."""
    await _require_agent(tenant_id, agent_id, registry_store)
    return await run_store.get_summary(tenant_id, agent_id, window_days)


@router.get("/{agent_id}/runs/{run_id}", response_model=RunRecord)
async def get_run(
    agent_id: str,
    run_id: str,
    tenant_id: Annotated[str, Depends(get_tenant_id)],
    _current_user: Annotated[CurrentUser, Depends(require_role(*_READ_ROLES))],
    registry_store: Annotated[AgentRegistryStore, Depends(get_registry_store)],
    run_store: Annotated[RunStore, Depends(get_run_store)],
) -> RunRecord:
    await _require_agent(tenant_id, agent_id, registry_store)
    run = await run_store.get_run(tenant_id, agent_id, run_id)
    if run is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"Run {run_id!r} not found"
        )
    return run


@router.get("/{agent_id}/runs/{run_id}/logs", response_model=LogListResponse)
async def get_run_logs(
    agent_id: str,
    run_id: str,
    tenant_id: Annotated[str, Depends(get_tenant_id)],
    _current_user: Annotated[CurrentUser, Depends(require_role(*_READ_ROLES))],
    registry_store: Annotated[AgentRegistryStore, Depends(get_registry_store)],
    run_store: Annotated[RunStore, Depends(get_run_store)],
) -> LogListResponse:
    await _require_agent(tenant_id, agent_id, registry_store)
    lines = await run_store.get_logs(tenant_id, agent_id, run_id)
    if lines is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"Run {run_id!r} not found"
        )
    return LogListResponse(lines=lines)


@router.post("/{agent_id}/runs/seed-demo", response_model=RunListResponse)
async def seed_demo_runs(
    agent_id: str,
    tenant_id: Annotated[str, Depends(get_tenant_id)],
    _current_user: Annotated[CurrentUser, Depends(require_role(*_WRITE_ROLES))],
    registry_store: Annotated[AgentRegistryStore, Depends(get_registry_store)],
    run_store: Annotated[RunStore, Depends(get_run_store)],
) -> RunListResponse:
    if not settings.seed_runs_enabled:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Demo run seeding is disabled on this deployment.",
        )
    agent = await registry_store.get_agent(tenant_id, agent_id)
    if agent is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"Agent {agent_id!r} not found"
        )
    items = await run_store.seed_demo_runs(
        tenant_id=tenant_id, agent_id=agent_id, version=agent.current_version
    )
    return RunListResponse(items=items)
