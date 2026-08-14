"""CloudWatch custom metrics (CLAUDE.md Section 14 Phase 14: "CloudWatch
metrics emitted from all key operations").

Emission is best-effort and never raises into the caller — a CloudWatch
outage or throttle must never fail an agent create/deploy/rollback. This is
purely an operational-metadata concern (R16/A7's ALLOWED_TELEMETRY_FIELDS
category, not customer data), so unlike Redis rate limiting (R39) there's no
security question here at all — failing open is simply the only sane
behaviour for a metrics sink.
"""

from __future__ import annotations

import asyncio
from typing import Any

from app.config import Settings
from app.shared.logging import get_logger

log = get_logger()


class MetricsEmitter:
    def __init__(self, cloudwatch_client: Any, settings: Settings) -> None:
        self._client = cloudwatch_client
        self._namespace = settings.cloudwatch_metrics_namespace

    async def emit(
        self,
        metric_name: str,
        value: float = 1.0,
        unit: str = "Count",
        dimensions: dict[str, str] | None = None,
    ) -> None:
        metric_data: dict[str, Any] = {
            "MetricName": metric_name,
            "Value": value,
            "Unit": unit,
        }
        if dimensions:
            metric_data["Dimensions"] = [{"Name": k, "Value": v} for k, v in dimensions.items()]

        try:
            await asyncio.to_thread(
                self._client.put_metric_data,
                Namespace=self._namespace,
                MetricData=[metric_data],
            )
        except Exception:
            log.warning("metrics.emit.failed", metric_name=metric_name, exc_info=True)
