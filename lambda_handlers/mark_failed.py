"""MarkFailed (CLAUDE.md Section 6.2 / R22) — reached via any state's
`Catch: States.ALL`. `event["error"]` is Step Functions' standard
{Error, Cause} shape from that Catch's ResultPath. Previous version stays
live (R22) via mark_deployment_failed's same fallback rule as
mark_deployment_blocked's.
"""

from __future__ import annotations

from typing import Any

from lambda_handlers.common import deployment_status_store, registry_store, require, run


def handler(event: dict[str, Any], _context: Any) -> dict[str, Any]:
    agent_id, tenant_id = require(event, "agentId", "tenantId")
    deployment_id = event["deploymentId"]

    error = event.get("error", {})
    reason = error.get("Cause") or error.get("Error") or "Deployment pipeline failed"

    run(
        registry_store.mark_deployment_failed(
            tenant_id=tenant_id, agent_id=agent_id, updated_by="step-functions"
        )
    )
    run(
        deployment_status_store.update_stage(
            agent_id=agent_id,
            deployment_id=deployment_id,
            stage=event.get("failedStage", "UNKNOWN"),
            stage_status="FAILED",
            output_summary=str(reason)[:2000],
            overall_status="FAILED",
            failure_reason=str(reason)[:2000],
            failed_stage=event.get("failedStage", "UNKNOWN"),
        )
    )
    return {"status": "FAILED", "reason": reason}
