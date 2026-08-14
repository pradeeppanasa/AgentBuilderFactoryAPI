"""DEPLOYING stage (CLAUDE.md Section 6.2: "ECS rolling update, waits for
tasks healthy").

PLACEHOLDER: verifying/forcing the *generated agent's own* ECS rollout
needs that agent's ECS cluster/service names, which no IaC template under
app/modules/iac_generator/templates emits as an output today — there's
nothing to look up yet. terraform apply (the APPLYING CodeBuild stage,
just before this) already triggers ECS's own rolling update declaratively
by virtue of updating the task definition/service; this stage's real job —
actively waiting for steady state and surfacing a timeout as a failure —
needs that output added first. See bootstrap/README.md.
"""

from __future__ import annotations

from typing import Any

from lambda_handlers.common import deployment_status_store, require, run


def handler(event: dict[str, Any], _context: Any) -> dict[str, Any]:
    agent_id, _tenant_id = require(event, "agentId", "tenantId")
    deployment_id = event["deploymentId"]

    run(
        deployment_status_store.update_stage(
            agent_id=agent_id,
            deployment_id=deployment_id,
            stage="DEPLOYING",
            stage_status="PASSED",
            output_summary=(
                "terraform apply already triggered the ECS rolling update; "
                "active steady-state polling is not wired in yet "
                "(no service name output — see module docstring)"
            ),
            overall_status="HEALTH_CHECK",
        )
    )
    return {"deployed": True}
