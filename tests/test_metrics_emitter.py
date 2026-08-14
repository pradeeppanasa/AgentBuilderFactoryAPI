"""Unit tests for app.modules.observability.metrics (CLAUDE.md Section 14)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import boto3
import pytest

from app.config import settings
from app.modules.observability.metrics import MetricsEmitter


@pytest.fixture
def cloudwatch_client() -> Any:
    return boto3.client("cloudwatch", region_name="eu-west-2")


async def test_emit_writes_a_metric_datapoint(cloudwatch_client: Any) -> None:
    emitter = MetricsEmitter(cloudwatch_client, settings)
    await emitter.emit("AgentCreated", dimensions={"tenant_id": "tenant-a"})

    now = datetime.now(UTC)
    stats = cloudwatch_client.get_metric_statistics(
        Namespace=settings.cloudwatch_metrics_namespace,
        MetricName="AgentCreated",
        Dimensions=[{"Name": "tenant_id", "Value": "tenant-a"}],
        StartTime=now - timedelta(minutes=5),
        EndTime=now + timedelta(minutes=5),
        Period=60,
        Statistics=["Sum"],
    )
    assert sum(dp["Sum"] for dp in stats["Datapoints"]) == 1.0


async def test_emit_defaults_to_count_unit_and_value_one(cloudwatch_client: Any) -> None:
    emitter = MetricsEmitter(cloudwatch_client, settings)
    await emitter.emit("AgentUpdated")  # no dimensions, no explicit value/unit

    now = datetime.now(UTC)
    stats = cloudwatch_client.get_metric_statistics(
        Namespace=settings.cloudwatch_metrics_namespace,
        MetricName="AgentUpdated",
        StartTime=now - timedelta(minutes=5),
        EndTime=now + timedelta(minutes=5),
        Period=60,
        Statistics=["Sum"],
    )
    assert sum(dp["Sum"] for dp in stats["Datapoints"]) == 1.0


async def test_emit_never_raises_when_cloudwatch_fails() -> None:
    class _ExplodingClient:
        def put_metric_data(self, **kwargs: Any) -> None:
            raise RuntimeError("CloudWatch is down")

    emitter = MetricsEmitter(_ExplodingClient(), settings)
    await emitter.emit("AgentCreated")  # must not raise
