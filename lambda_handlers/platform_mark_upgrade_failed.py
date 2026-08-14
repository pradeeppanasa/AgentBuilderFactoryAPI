"""MarkUpgradeFailed (Phase 15) — terminal failure. This IS "Platform
rollback: revert ECS task definition to previous image digest" (Section 14
Phase 15) — it isn't a separate user-triggered endpoint; ECS task
definition revisions are immutable, so reverting means pointing the
service back at previous_task_definition_arn, exactly the same
update_service call UpdatingService used to move forward.

A no-op (beyond recording the failure) if previous_task_definition_arn was
never set — PullingImage or an early RegisteringTaskDefinition failure
means the service was never touched, so there's nothing to roll back.
"""

from __future__ import annotations

import asyncio
from typing import Any

from app.config import settings
from lambda_handlers.common import ecs_client, platform_upgrade_store, require, run


async def _roll_back(previous_task_definition_arn: str) -> str:
    if not settings.ecs_cluster_name:
        raise RuntimeError("ECS_CLUSTER_NAME is not configured")
    await asyncio.to_thread(
        ecs_client.update_service,
        cluster=settings.ecs_cluster_name,
        service=settings.ecs_runtime_service_name,
        taskDefinition=previous_task_definition_arn,
    )
    return f"Rolled back to {previous_task_definition_arn}"


def handler(event: dict[str, Any], _context: Any) -> dict[str, Any]:
    (upgrade_id,) = require(event, "upgradeId")

    error = event.get("error", {})
    reason = error.get("Cause") or error.get("Error") or "Platform upgrade failed"
    failed_stage = event.get("failedStage", "UNKNOWN")

    record = run(platform_upgrade_store.get_upgrade(upgrade_id))
    rolled_back = False
    if record is not None and record.previous_task_definition_arn:
        run(_roll_back(record.previous_task_definition_arn))
        rolled_back = True

    run(
        platform_upgrade_store.update_stage(
            upgrade_id,
            failed_stage,
            "FAILED",
            output_summary=str(reason)[:2000],
            overall_status="ROLLED_BACK" if rolled_back else "FAILED",
            failure_reason=str(reason)[:2000],
            failed_stage=failed_stage,
        )
    )
    return {"status": "ROLLED_BACK" if rolled_back else "FAILED", "reason": reason}
