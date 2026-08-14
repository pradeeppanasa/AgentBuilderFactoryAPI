"""VALIDATING stage (CLAUDE.md Section 6.2 / A1 / A5).

Capability contract presence (R11) + a fresh circular-dependency check
(A5: "runs at... on deploy") — the version's own contract and its
orchestrator sub_agents were already validated once at *save* time
(app.modules.registry.store.AgentRegistryStore._validate_no_circular_dependency,
via create_agent/update_agent); this re-checks against the graph as it
stands *now*, since another agent's sub_agents could have changed since
this version was created, introducing a cycle this version wasn't aware of.
"""

from __future__ import annotations

from typing import Any

from app.modules.registry.dependency_validator import CircularDependencyValidator
from lambda_handlers.common import (
    StageFailure,
    deployment_status_store,
    registry_store,
    require,
    run,
)


async def _build_sub_agent_graph(tenant_id: str, exclude_agent_id: str) -> dict[str, list[str]]:
    graph: dict[str, list[str]] = {}
    cursor: str | None = None
    while True:
        records, cursor = await registry_store.list_agents(tenant_id, limit=100, cursor=cursor)
        for record in records:
            if record.agent_id == exclude_agent_id:
                continue
            version = await registry_store.get_version(record.agent_id, record.current_version)
            sub_agents: list[str] = []
            if version and version.configuration.orchestration:
                sub_agents = [
                    ref.agent_id for ref in version.configuration.orchestration.sub_agents
                ]
            graph[record.agent_id] = sub_agents
        if cursor is None:
            return graph


async def _validate(agent_id: str, tenant_id: str, version: int) -> dict[str, Any]:
    version_record = await registry_store.get_version_detail(tenant_id, agent_id, version)
    if version_record is None:
        raise StageFailure(f"Version {version} of agent {agent_id!r} not found")

    if version_record.capability_contract is None:
        raise StageFailure("capability_contract_missing")

    orchestration = version_record.configuration.orchestration
    if orchestration and orchestration.sub_agents:
        graph = await _build_sub_agent_graph(tenant_id, exclude_agent_id=agent_id)
        result = CircularDependencyValidator().validate(
            agent_id=agent_id,
            proposed_sub_agents=[ref.agent_id for ref in orchestration.sub_agents],
            all_agents=graph,
        )
        if not result.valid:
            raise StageFailure(result.reason or "circular_dependency_detected")

    return {"capabilityContractPresent": True}


def handler(event: dict[str, Any], _context: Any) -> dict[str, Any]:
    agent_id, tenant_id = require(event, "agentId", "tenantId")
    version = int(event["version"])
    deployment_id = event["deploymentId"]

    try:
        result = run(_validate(agent_id, tenant_id, version))
    except StageFailure as exc:
        run(
            deployment_status_store.update_stage(
                agent_id=agent_id,
                deployment_id=deployment_id,
                stage="VALIDATING",
                stage_status="FAILED",
                output_summary=str(exc),
                overall_status="FAILED",
                failure_reason=str(exc),
                failed_stage="VALIDATING",
            )
        )
        raise

    run(
        deployment_status_store.update_stage(
            agent_id=agent_id,
            deployment_id=deployment_id,
            stage="VALIDATING",
            stage_status="PASSED",
            output_summary="Capability contract present; no circular dependency",
            overall_status="CHANGE_IMPACT",
        )
    )
    return result
