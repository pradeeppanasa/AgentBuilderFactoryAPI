"""CHANGE_IMPACT stage (CLAUDE.md Section 7 / Phase 5 / R08).

Compares this version's configuration against the previous version's (or
None, on a first deployment) via the existing ChangeImpactAnalyzer. The
resulting impact_level is informational only per R08/F1 — it's recorded on
the stage and returned into the state machine's data for the UI to display
later, but nothing here can block the pipeline.
"""

from __future__ import annotations

from typing import Any

from app.modules.change_impact.analyzer import ChangeImpactAnalyzer
from lambda_handlers.common import (
    StageFailure,
    deployment_status_store,
    registry_store,
    require,
    run,
)

_analyzer = ChangeImpactAnalyzer()


async def _analyze(agent_id: str, tenant_id: str, version: int) -> dict[str, Any]:
    to_record = await registry_store.get_version_detail(tenant_id, agent_id, version)
    if to_record is None:
        raise StageFailure(f"Version {version} of agent {agent_id!r} not found")

    from_record = (
        await registry_store.get_version_detail(tenant_id, agent_id, version - 1)
        if version > 1
        else None
    )
    from_config = from_record.configuration if from_record else None

    analysis = _analyzer.analyze(from_config, to_record.configuration)
    return {
        "impactLevel": analysis.impact_level,
        "requiredValidations": analysis.required_validations,
        "matchedRules": analysis.matched_rules,
    }


def handler(event: dict[str, Any], _context: Any) -> dict[str, Any]:
    agent_id, tenant_id = require(event, "agentId", "tenantId")
    version = int(event["version"])
    deployment_id = event["deploymentId"]

    result = run(_analyze(agent_id, tenant_id, version))

    run(
        deployment_status_store.update_stage(
            agent_id=agent_id,
            deployment_id=deployment_id,
            stage="CHANGE_IMPACT",
            stage_status="PASSED",
            output_summary=(
                f"{result['impactLevel']} impact — "
                f"{len(result['requiredValidations'])} required validation(s)"
            ),
            overall_status="GENERATING_IAC",
        )
    )
    return result
