"""PULLING_IMAGE stage (Phase 15) — confirms the target tag/digest actually
exists in ECR before touching ECS at all. ECS itself pulls the image at
task-start time; this is purely a fail-fast validation gate."""

from __future__ import annotations

import asyncio
from typing import Any

from botocore.exceptions import ClientError

from app.modules.platform.version_service import repo_name_from_image_uri
from lambda_handlers.common import ecr_client, platform_upgrade_store, require, run


async def _confirm_image_exists(target_image: str) -> str:
    repo = repo_name_from_image_uri(target_image)
    if repo is None:
        raise RuntimeError(f"Not a recognizable ECR image URI: {target_image!r}")
    tag = target_image.rsplit(":", 1)[1]

    try:
        await asyncio.to_thread(
            ecr_client.describe_images, repositoryName=repo, imageIds=[{"imageTag": tag}]
        )
    except ClientError as exc:
        raise RuntimeError(f"Image {target_image!r} not found in ECR: {exc}") from exc

    return f"Confirmed {target_image} exists in ECR"


def handler(event: dict[str, Any], _context: Any) -> dict[str, Any]:
    upgrade_id, target_image = require(event, "upgradeId", "targetImage")

    try:
        summary = run(_confirm_image_exists(target_image))
    except Exception as exc:
        run(
            platform_upgrade_store.update_stage(
                upgrade_id,
                "PULLING_IMAGE",
                "FAILED",
                output_summary=str(exc),
                overall_status="FAILED",
                failure_reason=str(exc),
                failed_stage="PULLING_IMAGE",
            )
        )
        raise

    run(
        platform_upgrade_store.update_stage(
            upgrade_id,
            "PULLING_IMAGE",
            "PASSED",
            output_summary=summary,
            overall_status="PULLING_IMAGE",
        )
    )
    return {"imageConfirmed": True}
