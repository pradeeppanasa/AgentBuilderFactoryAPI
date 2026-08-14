"""Unit tests for app.modules.platform.upgrade_orchestrator (Phase 15).

Step Functions access goes through moto (conftest.py's autouse mocked_aws
fixture).
"""

from __future__ import annotations

import json

import boto3
import pytest

from app.config import settings
from app.modules.platform.upgrade_orchestrator import (
    PlatformUpgradeNotConfiguredError,
    PlatformUpgradeOrchestrator,
)

_DEFINITION = json.dumps({"StartAt": "A", "States": {"A": {"Type": "Pass", "End": True}}})


def _create_state_machine() -> str:
    sfn = boto3.client("stepfunctions", region_name="eu-west-2")
    response = sfn.create_state_machine(
        name="test-platform-upgrade",
        definition=_DEFINITION,
        roleArn="arn:aws:iam::123456789012:role/fake-deployment-role",
    )
    return str(response["stateMachineArn"])


async def test_start_upgrade_raises_when_not_configured() -> None:
    stub_settings = settings.model_copy(update={"platform_upgrade_state_machine_arn": None})
    orchestrator = PlatformUpgradeOrchestrator(
        boto3.client("stepfunctions", region_name="eu-west-2"), stub_settings
    )

    with pytest.raises(PlatformUpgradeNotConfiguredError):
        await orchestrator.start_upgrade(
            upgrade_id="UPG-NOPE",
            from_version="1.0.0",
            target_version="1.1.0",
            target_image="123456789012.dkr.ecr.eu-west-2.amazonaws.com/agent-builder-runtime:1.1.0",
        )


async def test_start_upgrade_returns_execution_arn_and_correct_input() -> None:
    state_machine_arn = _create_state_machine()
    stub_settings = settings.model_copy(
        update={"platform_upgrade_state_machine_arn": state_machine_arn}
    )
    sfn = boto3.client("stepfunctions", region_name="eu-west-2")
    orchestrator = PlatformUpgradeOrchestrator(sfn, stub_settings)

    execution_arn = await orchestrator.start_upgrade(
        upgrade_id="UPG-BBBB2222",
        from_version="1.0.0",
        target_version="1.1.0",
        target_image="123456789012.dkr.ecr.eu-west-2.amazonaws.com/agent-builder-runtime:1.1.0",
    )

    assert "UPG-BBBB2222" in execution_arn

    description = sfn.describe_execution(executionArn=execution_arn)
    assert description["stateMachineArn"] == state_machine_arn
    submitted_input = json.loads(description["input"])
    assert submitted_input == {
        "upgradeId": "UPG-BBBB2222",
        "fromVersion": "1.0.0",
        "targetVersion": "1.1.0",
        "targetImage": "123456789012.dkr.ecr.eu-west-2.amazonaws.com/agent-builder-runtime:1.1.0",
    }
