"""REGISTERING_TASK_DEFINITION stage (Phase 15) — clones the ECS service's
*currently running* task definition as a new, immutable revision with the
target image and PLATFORM_VERSION swapped in. ECS task definition revisions
can never be edited in place, only superseded — this is also what makes
rollback possible: the pre-upgrade ARN keeps working forever, so
"rollback" (platform_mark_upgrade_failed.py) is just pointing the service
back at it.
"""

from __future__ import annotations

import asyncio
from typing import Any

from app.config import settings
from lambda_handlers.common import ecs_client, platform_upgrade_store, require, run

_RUNTIME_CONTAINER_NAME = "agent-builder-runtime"

_TASK_DEF_COPY_KEYS = (
    "family",
    "taskRoleArn",
    "executionRoleArn",
    "networkMode",
    "containerDefinitions",
    "requiresCompatibilities",
    "cpu",
    "memory",
)


async def _register_new_revision(target_image: str, target_version: str) -> tuple[str, str]:
    if not settings.ecs_cluster_name:
        raise RuntimeError("ECS_CLUSTER_NAME is not configured")

    services = await asyncio.to_thread(
        ecs_client.describe_services,
        cluster=settings.ecs_cluster_name,
        services=[settings.ecs_runtime_service_name],
    )
    matches = services.get("services", [])
    if not matches:
        raise RuntimeError(
            f"ECS service {settings.ecs_runtime_service_name!r} not found in cluster "
            f"{settings.ecs_cluster_name!r}"
        )
    previous_task_definition_arn: str = matches[0]["taskDefinition"]

    current = await asyncio.to_thread(
        ecs_client.describe_task_definition, taskDefinition=previous_task_definition_arn
    )
    task_def = current["taskDefinition"]

    containers = []
    for container in task_def["containerDefinitions"]:
        container = dict(container)
        if container["name"] == _RUNTIME_CONTAINER_NAME:
            container["image"] = target_image
            env_vars = [dict(e) for e in container.get("environment", [])]
            for env_var in env_vars:
                if env_var["name"] == "PLATFORM_VERSION":
                    env_var["value"] = target_version
            container["environment"] = env_vars
        containers.append(container)

    register_kwargs = {k: task_def[k] for k in _TASK_DEF_COPY_KEYS if k in task_def}
    register_kwargs["containerDefinitions"] = containers

    registered = await asyncio.to_thread(ecs_client.register_task_definition, **register_kwargs)
    new_task_definition_arn: str = registered["taskDefinition"]["taskDefinitionArn"]

    return previous_task_definition_arn, new_task_definition_arn


def handler(event: dict[str, Any], _context: Any) -> dict[str, Any]:
    upgrade_id, target_image, target_version = require(
        event, "upgradeId", "targetImage", "targetVersion"
    )

    try:
        previous_arn, new_arn = run(_register_new_revision(target_image, target_version))
    except Exception as exc:
        run(
            platform_upgrade_store.update_stage(
                upgrade_id,
                "REGISTERING_TASK_DEFINITION",
                "FAILED",
                output_summary=str(exc),
                overall_status="FAILED",
                failure_reason=str(exc),
                failed_stage="REGISTERING_TASK_DEFINITION",
            )
        )
        raise

    run(
        platform_upgrade_store.update_stage(
            upgrade_id,
            "REGISTERING_TASK_DEFINITION",
            "PASSED",
            output_summary=f"Registered {new_arn} (previous: {previous_arn})",
            overall_status="REGISTERING_TASK_DEFINITION",
            previous_task_definition_arn=previous_arn,
            new_task_definition_arn=new_arn,
        )
    )
    return {"previousTaskDefinitionArn": previous_arn, "newTaskDefinitionArn": new_arn}
