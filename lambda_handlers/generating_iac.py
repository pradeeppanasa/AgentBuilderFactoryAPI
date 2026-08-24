"""GENERATING_IAC stage (CLAUDE.md Section 8 / Section 10 / F5 / Section 45.2).

Section 45.2/45.3's per-agent-repo, v1-direct/v2+-PR git flow now lives in
app/api/v1/agents.py::_trigger_deployment — it runs synchronously, before
this Step Functions execution is even started (deployment_orchestrator.
trigger_deployment publishes the "AgentDeploymentRequested" event as the
LAST step of that route, after the IaC has already been generated and
pushed). Re-running the same generate + branch + commit + PR dance here
would either 422 against a real git host (the branch/PR already exists) or
silently duplicate it — this stage is therefore a thin pass-through that
reads back what agents.py already recorded and confirms the stage as
PASSED, rather than a second implementation of the same git flow.
"""

from __future__ import annotations

from typing import Any

from lambda_handlers.common import (
    StageFailure,
    deployment_status_store,
    registry_store,
    require,
    run,
)


async def _confirm_iac_already_generated(
    agent_id: str, tenant_id: str, version: int, deployment_id: str
) -> dict[str, Any]:
    version_record = await registry_store.get_version_detail(tenant_id, agent_id, version)
    if version_record is None:
        raise StageFailure(f"Version {version} of agent {agent_id!r} not found")
    if not version_record.iac_s3_key:
        raise StageFailure(
            f"Version {version} of agent {agent_id!r} has no IaC artifact recorded — "
            "expected app/api/v1/agents.py's deploy trigger to have generated and "
            "pushed it before publishing this deployment's event"
        )

    deployment = await deployment_status_store.get_deployment(agent_id, deployment_id)
    if deployment is None:
        raise StageFailure(f"Deployment {deployment_id!r} not found for agent {agent_id!r}")

    return {
        "iacVersion": version_record.iac_version,
        "s3Key": version_record.iac_s3_key,
        "modules": version_record.iac_modules or [],
        "branch": deployment.branch,
        "prId": deployment.pull_request_id,
    }


def handler(event: dict[str, Any], _context: Any) -> dict[str, Any]:
    agent_id, tenant_id = require(event, "agentId", "tenantId")
    version = int(event["version"])
    deployment_id = event["deploymentId"]

    result = run(_confirm_iac_already_generated(agent_id, tenant_id, version, deployment_id))

    run(
        deployment_status_store.update_stage(
            agent_id=agent_id,
            deployment_id=deployment_id,
            stage="GENERATING_IAC",
            stage_status="PASSED",
            output_summary=(
                f"Terraform already generated and pushed by the deploy trigger: "
                f"{len(result['modules'])} module(s)"
                + (f", PR {result['prId']} opened" if result["prId"] else " (pushed to main)")
            ),
            overall_status="SECURITY_SCANNING",
        )
    )
    return result
