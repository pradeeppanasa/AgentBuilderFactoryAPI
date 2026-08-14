"""Unit tests for DeploymentStatusStore against moto's DynamoDB mock."""

from __future__ import annotations

import boto3
import pytest

from app.config import settings
from app.modules.deployment.models import DeploymentRecord, initial_stages
from app.modules.deployment.status_store import DeploymentNotFoundError, DeploymentStatusStore


def _record(agent_id: str, deployment_id: str, triggered_at: str) -> DeploymentRecord:
    return DeploymentRecord(
        agent_id=agent_id,
        deployment_id=deployment_id,
        version=1,
        triggered_by="dev@example.com",
        triggered_at=triggered_at,
        stages=initial_stages(),
        iac_s3_key="iac/terraform/agent-1/v1/agent-1-v1-1.0.1.zip",
        updated_at=triggered_at,
    )


@pytest.fixture
async def store() -> DeploymentStatusStore:
    dynamodb = boto3.resource("dynamodb", region_name="eu-west-2")
    status_store = DeploymentStatusStore(dynamodb, settings)
    await status_store.ensure_table()
    return status_store


async def test_create_and_get_deployment(store: DeploymentStatusStore) -> None:
    record = _record("agent-1", "DEP-AAAAAAAA", "2026-01-01T00:00:00+00:00")
    await store.create_deployment(record)

    fetched = await store.get_deployment("agent-1", "DEP-AAAAAAAA")
    assert fetched is not None
    assert fetched.deployment_id == "DEP-AAAAAAAA"
    assert fetched.status == "PENDING"
    assert set(fetched.stages) == {
        "VALIDATING",
        "CHANGE_IMPACT",
        "GENERATING_IAC",
        "SECURITY_SCANNING",
        "EVALUATING",
        "TERRAFORM_VALIDATE",
        "TERRAFORM_PLAN",
        "POLICY_CHECK",
        "APPLYING",
        "DEPLOYING",
        "HEALTH_CHECK",
    }
    assert all(stage.status == "PENDING" for stage in fetched.stages.values())
    assert fetched.iac_s3_key == record.iac_s3_key


async def test_get_deployment_returns_none_for_unknown(store: DeploymentStatusStore) -> None:
    assert await store.get_deployment("agent-1", "DEP-MISSING") is None


async def test_get_deployment_by_id_resolves_via_gsi(store: DeploymentStatusStore) -> None:
    record = _record("agent-2", "DEP-BBBBBBBB", "2026-01-02T00:00:00+00:00")
    await store.create_deployment(record)

    fetched = await store.get_deployment_by_id("DEP-BBBBBBBB")
    assert fetched is not None
    assert fetched.agent_id == "agent-2"

    assert await store.get_deployment_by_id("DEP-DOES-NOT-EXIST") is None


async def test_list_deployments_sorted_newest_first(store: DeploymentStatusStore) -> None:
    await store.create_deployment(_record("agent-3", "DEP-1", "2026-01-01T00:00:00+00:00"))
    await store.create_deployment(_record("agent-3", "DEP-2", "2026-01-03T00:00:00+00:00"))
    await store.create_deployment(_record("agent-3", "DEP-3", "2026-01-02T00:00:00+00:00"))

    deployments = await store.list_deployments("agent-3")

    assert [d.deployment_id for d in deployments] == ["DEP-2", "DEP-3", "DEP-1"]


async def test_list_deployments_scoped_to_agent(store: DeploymentStatusStore) -> None:
    await store.create_deployment(_record("agent-4", "DEP-X", "2026-01-01T00:00:00+00:00"))
    await store.create_deployment(_record("agent-5", "DEP-Y", "2026-01-01T00:00:00+00:00"))

    assert [d.deployment_id for d in await store.list_deployments("agent-4")] == ["DEP-X"]
    assert [d.deployment_id for d in await store.list_deployments("agent-5")] == ["DEP-Y"]


async def test_update_stage_sets_status_and_summary(store: DeploymentStatusStore) -> None:
    await store.create_deployment(_record("agent-6", "DEP-6", "2026-01-01T00:00:00+00:00"))

    updated = await store.update_stage(
        "agent-6",
        "DEP-6",
        "TERRAFORM_VALIDATE",
        "PASSED",
        output_summary="terraform validate passed",
    )

    assert updated.stages["TERRAFORM_VALIDATE"].status == "PASSED"
    assert updated.stages["TERRAFORM_VALIDATE"].output_summary == "terraform validate passed"
    assert updated.stages["TERRAFORM_VALIDATE"].completed_at is not None
    assert updated.current_stage == "TERRAFORM_VALIDATE"
    # Untouched stages remain PENDING.
    assert updated.stages["TERRAFORM_PLAN"].status == "PENDING"

    persisted = await store.get_deployment("agent-6", "DEP-6")
    assert persisted is not None
    assert persisted.stages["TERRAFORM_VALIDATE"].status == "PASSED"


async def test_update_stage_can_set_overall_status_and_failure_reason(
    store: DeploymentStatusStore,
) -> None:
    await store.create_deployment(_record("agent-7", "DEP-7", "2026-01-01T00:00:00+00:00"))

    updated = await store.update_stage(
        "agent-7",
        "DEP-7",
        "POLICY_CHECK",
        "BLOCKED",
        output_summary="Critical finding: hardcoded secret",
        blocking_issue="hardcoded_secret_found",
        overall_status="BLOCKED",
        failure_reason="Critical finding: hardcoded secret",
        failed_stage="POLICY_CHECK",
    )

    assert updated.status == "BLOCKED"
    assert updated.failure_reason == "Critical finding: hardcoded secret"
    assert updated.failed_stage == "POLICY_CHECK"
    assert updated.stages["POLICY_CHECK"].blocking_issue == "hardcoded_secret_found"


async def test_update_stage_raises_for_unknown_deployment(store: DeploymentStatusStore) -> None:
    with pytest.raises(DeploymentNotFoundError):
        await store.update_stage("agent-x", "DEP-MISSING", "VALIDATING", "PASSED")
