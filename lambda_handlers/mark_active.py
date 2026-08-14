"""MarkActive (CLAUDE.md Section 6.2 / R22): "Previous version gracefully
drained. New version live." — the one place live_version actually advances.
"""

from __future__ import annotations

from typing import Any

from lambda_handlers.common import deployment_status_store, registry_store, require, run


def handler(event: dict[str, Any], _context: Any) -> dict[str, Any]:
    agent_id, tenant_id = require(event, "agentId", "tenantId")
    version = int(event["version"])
    deployment_id = event["deploymentId"]

    run(
        registry_store.mark_deployment_active(
            tenant_id=tenant_id,
            agent_id=agent_id,
            live_version=version,
            updated_by="step-functions",
        )
    )
    run(
        deployment_status_store.update_stage(
            agent_id=agent_id,
            deployment_id=deployment_id,
            stage="HEALTH_CHECK",
            stage_status="PASSED",
            overall_status="ACTIVE",
        )
    )
    return {"status": "ACTIVE", "liveVersion": version}
