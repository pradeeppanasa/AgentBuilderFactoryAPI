"""HEALTH_CHECK stage (CLAUDE.md Section 6.2 / Section 4.4's health_check_url).

Delegates the actual probe to app.modules.deployment.health_check.HealthChecker
(POST + status + version check, Phase 13) — this handler only resolves
health_check_url/expected version from the deployment record and persists
the result, same "thin wrapper" shape as every other handler in this
package. Honest placeholder only when health_check_url itself is unset — no
IaC template emits that URL as an output yet (see that module's own note).
"""

from __future__ import annotations

from typing import Any

from app.modules.deployment.health_check import HealthChecker
from lambda_handlers.common import deployment_status_store, require, run

_checker = HealthChecker()


async def _check(agent_id: str, deployment_id: str) -> tuple[bool, str]:
    deployment = await deployment_status_store.get_deployment(agent_id, deployment_id)
    if deployment is None or not deployment.health_check_url:
        return True, "No health_check_url recorded for this deployment yet — see module docstring"

    result = await _checker.check(
        deployment.health_check_url, expected_version=str(deployment.version)
    )
    return result.passed, result.summary


def handler(event: dict[str, Any], _context: Any) -> dict[str, Any]:
    agent_id, _tenant_id = require(event, "agentId", "tenantId")
    deployment_id = event["deploymentId"]

    passed, summary = run(_check(agent_id, deployment_id))

    if not passed:
        run(
            deployment_status_store.update_stage(
                agent_id=agent_id,
                deployment_id=deployment_id,
                stage="HEALTH_CHECK",
                stage_status="FAILED",
                output_summary=summary,
                overall_status="FAILED",
                failure_reason=summary,
                failed_stage="HEALTH_CHECK",
            )
        )
        raise RuntimeError(summary)

    run(
        deployment_status_store.update_stage(
            agent_id=agent_id,
            deployment_id=deployment_id,
            stage="HEALTH_CHECK",
            stage_status="PASSED",
            output_summary=summary,
            overall_status="ACTIVE",
        )
    )
    return {"healthCheckPassed": True}
