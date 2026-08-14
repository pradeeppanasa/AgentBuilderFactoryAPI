"""Unit tests for app.modules.platform.upgrade_store (Phase 15).

DynamoDB access goes through moto (conftest.py's autouse mocked_aws
fixture) — same pattern as tests/test_deployment_status_store.py.
"""

from __future__ import annotations

import boto3
import pytest

from app.config import settings
from app.modules.platform.upgrade_models import PlatformUpgradeRecord, initial_upgrade_stages
from app.modules.platform.upgrade_store import PlatformUpgradeStatusStore, UpgradeNotFoundError


@pytest.fixture
async def store() -> PlatformUpgradeStatusStore:
    dynamodb = boto3.resource("dynamodb", region_name="eu-west-2")
    upgrade_store = PlatformUpgradeStatusStore(dynamodb, settings)
    await upgrade_store.ensure_table()
    return upgrade_store


def _record(
    upgrade_id: str, triggered_at: str = "2026-08-14T00:00:00+00:00"
) -> PlatformUpgradeRecord:
    return PlatformUpgradeRecord(
        upgrade_id=upgrade_id,
        from_version="1.0.0",
        target_version="1.1.0",
        target_image="123456789012.dkr.ecr.eu-west-2.amazonaws.com/agent-builder-runtime:1.1.0",
        stages=initial_upgrade_stages(),
        triggered_by="admin@example.com",
        triggered_at=triggered_at,
        updated_at=triggered_at,
    )


async def test_ensure_table_is_idempotent(store: PlatformUpgradeStatusStore) -> None:
    await store.ensure_table()  # second call must not raise (ResourceInUseException swallowed)


async def test_create_and_get_upgrade_round_trip(store: PlatformUpgradeStatusStore) -> None:
    record = _record("UPG-AAAA1111")
    await store.create_upgrade(record)

    fetched = await store.get_upgrade("UPG-AAAA1111")

    assert fetched is not None
    assert fetched.upgrade_id == "UPG-AAAA1111"
    assert fetched.from_version == "1.0.0"
    assert fetched.target_version == "1.1.0"
    assert fetched.status == "PENDING"
    assert set(fetched.stages.keys()) == {
        "PULLING_IMAGE",
        "REGISTERING_TASK_DEFINITION",
        "UPDATING_SERVICE",
        "HEALTH_CHECK",
    }


async def test_get_upgrade_returns_none_when_missing(store: PlatformUpgradeStatusStore) -> None:
    assert await store.get_upgrade("does-not-exist") is None


async def test_list_upgrades_sorted_newest_first(store: PlatformUpgradeStatusStore) -> None:
    await store.create_upgrade(_record("UPG-OLD", triggered_at="2026-01-01T00:00:00+00:00"))
    await store.create_upgrade(_record("UPG-NEW", triggered_at="2026-06-01T00:00:00+00:00"))

    upgrades = await store.list_upgrades()

    assert [u.upgrade_id for u in upgrades] == ["UPG-NEW", "UPG-OLD"]


async def test_update_stage_raises_when_upgrade_missing(store: PlatformUpgradeStatusStore) -> None:
    with pytest.raises(UpgradeNotFoundError):
        await store.update_stage("does-not-exist", "PULLING_IMAGE", "RUNNING")


async def test_update_stage_running_sets_started_at(store: PlatformUpgradeStatusStore) -> None:
    await store.create_upgrade(_record("UPG-STAGE1"))

    updated = await store.update_stage("UPG-STAGE1", "PULLING_IMAGE", "RUNNING")

    stage = updated.stages["PULLING_IMAGE"]
    assert stage.status == "RUNNING"
    assert stage.started_at is not None
    assert stage.completed_at is None
    assert updated.current_stage == "PULLING_IMAGE"


async def test_update_stage_passed_sets_completed_at_and_output_summary(
    store: PlatformUpgradeStatusStore,
) -> None:
    await store.create_upgrade(_record("UPG-STAGE2"))
    await store.update_stage("UPG-STAGE2", "PULLING_IMAGE", "RUNNING")

    updated = await store.update_stage(
        "UPG-STAGE2", "PULLING_IMAGE", "PASSED", output_summary="Image confirmed in ECR"
    )

    stage = updated.stages["PULLING_IMAGE"]
    assert stage.status == "PASSED"
    assert stage.started_at is not None
    assert stage.completed_at is not None
    assert stage.output_summary == "Image confirmed in ECR"


async def test_update_stage_can_set_overall_status_and_failure_reason(
    store: PlatformUpgradeStatusStore,
) -> None:
    await store.create_upgrade(_record("UPG-STAGE3"))

    updated = await store.update_stage(
        "UPG-STAGE3",
        "UPDATING_SERVICE",
        "FAILED",
        overall_status="FAILED",
        failure_reason="ECS service did not stabilise",
        failed_stage="UPDATING_SERVICE",
    )

    assert updated.status == "FAILED"
    assert updated.failure_reason == "ECS service did not stabilise"
    assert updated.failed_stage == "UPDATING_SERVICE"


async def test_update_stage_records_task_definition_arns(
    store: PlatformUpgradeStatusStore,
) -> None:
    await store.create_upgrade(_record("UPG-STAGE4"))

    updated = await store.update_stage(
        "UPG-STAGE4",
        "REGISTERING_TASK_DEFINITION",
        "PASSED",
        previous_task_definition_arn="arn:aws:ecs:eu-west-2:123456789012:task-definition/runtime:3",
        new_task_definition_arn="arn:aws:ecs:eu-west-2:123456789012:task-definition/runtime:4",
    )

    assert updated.previous_task_definition_arn == (
        "arn:aws:ecs:eu-west-2:123456789012:task-definition/runtime:3"
    )
    assert updated.new_task_definition_arn == (
        "arn:aws:ecs:eu-west-2:123456789012:task-definition/runtime:4"
    )

    # Persisted, not just returned — refetch to be sure.
    refetched = await store.get_upgrade("UPG-STAGE4")
    assert refetched is not None
    assert refetched.new_task_definition_arn == updated.new_task_definition_arn


async def test_update_stage_preserves_other_stages(store: PlatformUpgradeStatusStore) -> None:
    await store.create_upgrade(_record("UPG-STAGE5"))

    await store.update_stage("UPG-STAGE5", "PULLING_IMAGE", "PASSED")
    updated = await store.update_stage("UPG-STAGE5", "REGISTERING_TASK_DEFINITION", "RUNNING")

    assert updated.stages["PULLING_IMAGE"].status == "PASSED"
    assert updated.stages["REGISTERING_TASK_DEFINITION"].status == "RUNNING"
    assert updated.stages["UPDATING_SERVICE"].status == "PENDING"
