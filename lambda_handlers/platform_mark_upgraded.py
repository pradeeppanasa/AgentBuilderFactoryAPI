"""MarkUpgraded (Phase 15) — terminal success. Reuses the HEALTH_CHECK
stage entry rather than adding a new one, same pattern as
lambda_handlers.mark_active reusing HEALTH_CHECK for the agent pipeline's
terminal flip to ACTIVE.
"""

from __future__ import annotations

from typing import Any

from lambda_handlers.common import platform_upgrade_store, require, run


def handler(event: dict[str, Any], _context: Any) -> dict[str, Any]:
    (upgrade_id,) = require(event, "upgradeId")

    run(
        platform_upgrade_store.update_stage(
            upgrade_id,
            "HEALTH_CHECK",
            "PASSED",
            overall_status="ACTIVE",
        )
    )
    return {"status": "ACTIVE"}
