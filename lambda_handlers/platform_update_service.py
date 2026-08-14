"""UPDATING_SERVICE stage (Phase 15) — points the ECS service at the new
task definition revision. ECS's own deployment configuration
(minimumHealthyPercent/maximumPercent, set explicitly in
bootstrap/stage1/ecs.tf) performs the actual zero-downtime rolling
replacement; this stage just triggers it and polls until the service
reports the new revision fully rolled out.

A short bounded poll rather than botocore's `services_stable` waiter — that
waiter's default cadence (every 15s, up to 40 attempts = 10 minutes) is
sized for a real rolling deployment, but this handler's own Lambda timeout
has to cover it, and moto's ECS simulation doesn't model a gradual rollout
at all (it settles state on the update_service call itself), so a long
waiter would only ever add latency here, never signal.
"""

from __future__ import annotations

import asyncio
from typing import Any

from app.config import settings
from lambda_handlers.common import ecs_client, platform_upgrade_store, require, run

_POLL_ATTEMPTS = 20
_POLL_DELAY_SECONDS = 15


async def _update_and_wait_stable(task_definition_arn: str) -> str:
    if not settings.ecs_cluster_name:
        raise RuntimeError("ECS_CLUSTER_NAME is not configured")

    await asyncio.to_thread(
        ecs_client.update_service,
        cluster=settings.ecs_cluster_name,
        service=settings.ecs_runtime_service_name,
        taskDefinition=task_definition_arn,
    )

    for attempt in range(_POLL_ATTEMPTS):
        response = await asyncio.to_thread(
            ecs_client.describe_services,
            cluster=settings.ecs_cluster_name,
            services=[settings.ecs_runtime_service_name],
        )
        service = response["services"][0]
        if (
            service["taskDefinition"] == task_definition_arn
            and service["runningCount"] == service["desiredCount"]
        ):
            return (
                f"Service stable on {task_definition_arn} "
                f"({service['runningCount']}/{service['desiredCount']} tasks)"
            )
        if attempt < _POLL_ATTEMPTS - 1:
            await asyncio.sleep(_POLL_DELAY_SECONDS)

    raise RuntimeError(
        f"Service did not stabilize on {task_definition_arn} after {_POLL_ATTEMPTS} attempts"
    )


def handler(event: dict[str, Any], _context: Any) -> dict[str, Any]:
    (upgrade_id,) = require(event, "upgradeId")
    new_task_definition_arn = event["registeringTaskDefinition"]["newTaskDefinitionArn"]

    try:
        summary = run(_update_and_wait_stable(new_task_definition_arn))
    except Exception as exc:
        run(
            platform_upgrade_store.update_stage(
                upgrade_id,
                "UPDATING_SERVICE",
                "FAILED",
                output_summary=str(exc),
                overall_status="FAILED",
                failure_reason=str(exc),
                failed_stage="UPDATING_SERVICE",
            )
        )
        raise

    run(
        platform_upgrade_store.update_stage(
            upgrade_id,
            "UPDATING_SERVICE",
            "PASSED",
            output_summary=summary,
            overall_status="UPDATING_SERVICE",
        )
    )
    return {"serviceUpdated": True}
