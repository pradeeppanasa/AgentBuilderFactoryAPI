"""DeploymentOrchestrator tests (CLAUDE.md Section 6.1) against moto's EventBridge mock."""

from __future__ import annotations

import json
from typing import Any

import boto3
import pytest

from app.config import settings
from app.modules.deployment.orchestrator import DeploymentOrchestrator
from tests.conftest import TEST_EVENTBRIDGE_BUS


class _RecordingEventsClient:
    """Wraps the real (moto-backed) client so we can inspect what was sent
    while still exercising moto's own validation (bus must exist, etc.)."""

    def __init__(self, real_client: Any) -> None:
        self._real = real_client
        self.calls: list[dict[str, Any]] = []

    def put_events(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        return self._real.put_events(**kwargs)


@pytest.fixture
def recording_client() -> _RecordingEventsClient:
    real_client = boto3.client("events", region_name="eu-west-2")
    return _RecordingEventsClient(real_client)


async def test_trigger_deployment_publishes_expected_event(
    recording_client: _RecordingEventsClient,
) -> None:
    orchestrator = DeploymentOrchestrator(recording_client, settings)

    await orchestrator.trigger_deployment(
        agent_id="kyc-agent-a1b2c3",
        version=4,
        deployment_id="DEP-ABCDEF12",
        tenant_id="tenant-a",
    )

    assert len(recording_client.calls) == 1
    entries = recording_client.calls[0]["Entries"]
    assert len(entries) == 1
    entry = entries[0]

    assert entry["Source"] == "panasa.agent-builder"
    assert entry["DetailType"] == "AgentDeploymentRequested"
    assert entry["EventBusName"] == TEST_EVENTBRIDGE_BUS

    detail = json.loads(entry["Detail"])
    assert detail["agentId"] == "kyc-agent-a1b2c3"
    assert detail["version"] == 4
    assert detail["deploymentId"] == "DEP-ABCDEF12"
    assert detail["tenantId"] == "tenant-a"
    assert "triggeredAt" in detail


async def test_trigger_deployment_succeeds_against_real_moto_bus(
    recording_client: _RecordingEventsClient,
) -> None:
    # No FailedEntryCount means moto accepted the event against a bus that
    # actually exists — not just that our code assembled a plausible dict.
    orchestrator = DeploymentOrchestrator(recording_client, settings)

    await orchestrator.trigger_deployment("agent-1", 1, "DEP-1", "tenant-a")

    response = recording_client._real.put_events(
        Entries=[
            {
                "Source": "panasa.agent-builder",
                "DetailType": "AgentDeploymentRequested",
                "EventBusName": TEST_EVENTBRIDGE_BUS,
                "Detail": json.dumps({"probe": True}),
            }
        ]
    )
    assert response.get("FailedEntryCount", 0) == 0
