"""HEALTH_CHECK stage (Phase 15) — GET on the Runtime's own
/api/v1/platform/health (Section 5.6), confirming the newly-rolled-out
version is actually serving before declaring the upgrade a success.

Deliberately its own small check rather than reusing
app.modules.deployment.health_check.HealthChecker — that one POSTs, per
F1's explicit (if unusual) wording for *agent* health checks; this
Runtime's own health endpoint has always been a GET (Section 5.6, and the
actual `@router.get("/health")` in app/api/v1/platform.py). Conflating the
two would mean silently changing one of them to match the other.
"""

from __future__ import annotations

from typing import Any

import httpx

from app.config import settings
from lambda_handlers.common import platform_upgrade_store, require, run

_TIMEOUT_SECONDS = 10.0


async def _check(target_version: str) -> str:
    if not settings.platform_health_check_url:
        raise RuntimeError("PLATFORM_HEALTH_CHECK_URL is not configured")

    async with httpx.AsyncClient(timeout=_TIMEOUT_SECONDS) as client:
        response = await client.get(settings.platform_health_check_url)

    if response.status_code != 200:
        raise RuntimeError(f"GET {settings.platform_health_check_url} -> {response.status_code}")

    body = response.json()
    actual_version = body.get("version")
    if str(actual_version) != str(target_version):
        raise RuntimeError(
            f"Version mismatch after upgrade: expected {target_version!r}, "
            f"got {actual_version!r}"
        )

    return f"GET {settings.platform_health_check_url} -> 200, version {target_version} confirmed"


def handler(event: dict[str, Any], _context: Any) -> dict[str, Any]:
    upgrade_id, target_version = require(event, "upgradeId", "targetVersion")

    try:
        summary = run(_check(target_version))
    except Exception as exc:
        run(
            platform_upgrade_store.update_stage(
                upgrade_id,
                "HEALTH_CHECK",
                "FAILED",
                output_summary=str(exc),
                overall_status="FAILED",
                failure_reason=str(exc),
                failed_stage="HEALTH_CHECK",
            )
        )
        raise

    run(
        platform_upgrade_store.update_stage(
            upgrade_id,
            "HEALTH_CHECK",
            "PASSED",
            output_summary=summary,
            overall_status="HEALTH_CHECK",
        )
    )
    return {"healthCheckPassed": True}
