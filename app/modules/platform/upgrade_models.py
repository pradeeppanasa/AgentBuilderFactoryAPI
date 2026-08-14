"""Platform upgrade models (CLAUDE.md Section 14 Phase 15 / Section 5.6).

Not tenant-scoped (no tenant_id) — unlike every agent-related table (R01),
there is exactly one Factory Runtime deployment per bootstrapped instance,
regardless of how many tenants use it in prototype mode. A platform
upgrade/rollback is operational metadata about that one shared deployment,
not tenant data.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

UpgradeStageName = Literal[
    "PULLING_IMAGE",
    "REGISTERING_TASK_DEFINITION",
    "UPDATING_SERVICE",
    "HEALTH_CHECK",
]

UPGRADE_STAGE_ORDER: list[str] = [
    "PULLING_IMAGE",
    "REGISTERING_TASK_DEFINITION",
    "UPDATING_SERVICE",
    "HEALTH_CHECK",
]

UpgradeStatus = Literal[
    "PENDING",
    "PULLING_IMAGE",
    "REGISTERING_TASK_DEFINITION",
    "UPDATING_SERVICE",
    "HEALTH_CHECK",
    "ACTIVE",
    "FAILED",
    "ROLLED_BACK",
]

UpgradeStageStatus = Literal["PENDING", "RUNNING", "PASSED", "FAILED"]


class UpgradeStageResult(BaseModel):
    stage: str
    status: UpgradeStageStatus = "PENDING"
    started_at: str | None = None
    completed_at: str | None = None
    output_summary: str | None = None


class PlatformUpgradeRecord(BaseModel):
    upgrade_id: str

    from_version: str
    target_version: str
    target_image: str

    # Populated by REGISTERING_TASK_DEFINITION; consumed by UPDATING_SERVICE
    # and by the rollback path in MarkUpgradeFailed — this pair is the whole
    # "Platform rollback: revert ECS task definition to previous image
    # digest" mechanism (ECS task definition revisions are immutable, so
    # "rollback" is just pointing the service back at the old ARN).
    previous_task_definition_arn: str | None = None
    new_task_definition_arn: str | None = None

    status: UpgradeStatus = "PENDING"
    current_stage: str = UPGRADE_STAGE_ORDER[0]
    stages: dict[str, UpgradeStageResult] = Field(default_factory=dict)

    triggered_by: str
    triggered_at: str

    failure_reason: str | None = None
    failed_stage: str | None = None

    updated_at: str


def initial_upgrade_stages() -> dict[str, UpgradeStageResult]:
    return {stage: UpgradeStageResult(stage=stage) for stage in UPGRADE_STAGE_ORDER}
