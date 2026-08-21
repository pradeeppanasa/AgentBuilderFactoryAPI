"""Observability Capability Discovery.

The platform discovers observability capabilities from the deployment's
registered configuration (`PlatformSettingsRecord`) and reports them to the
UI using provider-neutral capability names — "logs", "metrics",
"distributed_tracing", "opentelemetry" — never a specific vendor name as the
thing the UI keys its logic off. Provider-specific wiring is resolved by the
small set of `_*Adapter` classes below: each adapter is the ONLY place that
is allowed to know a provider's identity, and it maps that knowledge onto
one neutral capability plus a human-readable detail string. The core UI
renders `capability`/`status`/`detail` and treats `adapters` (a list of
opaque adapter ids) as informational disclosure text only — never as
something it branches on.

Status honesty (matches DefaultStackStatus's existing precedent in
admin_settings.py — "claiming active here would be a false statement"):
  "active"   — genuinely wired and running, Panasa-operated infrastructure
               (CloudWatch Logs/Metrics, AWS X-Ray).
  "unknown"  — configured by the tenant but not actively health-probed
               (an OTel collector endpoint, or a self-hosted integration).
  "inactive" — not configured at all.
"active" always outranks "unknown" outranks "inactive" when more than one
adapter contributes to the same capability (e.g. X-Ray plus Langfuse both
feed "distributed_tracing").
"""

from __future__ import annotations

from typing import Literal, Protocol

from pydantic import BaseModel, Field

from app.modules.platform_settings.models import PlatformSettingsRecord

CapabilityKind = Literal["logs", "metrics", "distributed_tracing", "opentelemetry"]
CapabilityStatus = Literal["active", "inactive", "unknown"]

_STATUS_RANK: dict[CapabilityStatus, int] = {"active": 2, "unknown": 1, "inactive": 0}

_CAPABILITY_ORDER: tuple[CapabilityKind, ...] = (
    "logs",
    "metrics",
    "distributed_tracing",
    "opentelemetry",
)


class ObservabilityCapability(BaseModel):
    capability: CapabilityKind
    status: CapabilityStatus
    detail: str
    adapters: list[str] = Field(default_factory=list)
    """Opaque infrastructure-adapter ids that contributed to this capability
    (e.g. "cloudwatch_logs", "langfuse"). Informational disclosure only —
    the UI must not branch on these, only display them."""


class CapabilityDiscoveryResponse(BaseModel):
    capabilities: list[ObservabilityCapability]


class ObservabilityAdapter(Protocol):
    """One adapter per provider. The only layer permitted to know a
    provider's name — everything above this resolves() call is neutral."""

    adapter_id: str
    capability: CapabilityKind

    def resolve(self, config: PlatformSettingsRecord) -> tuple[CapabilityStatus, str] | None:
        """Returns (status, detail), or None if this adapter has nothing to
        contribute (e.g. an optional integration the tenant never enabled)."""
        ...


# ── Always-on, Panasa-operated infrastructure (R46 default stack) ──────────


class _CloudWatchLogsAdapter:
    adapter_id = "cloudwatch_logs"
    capability: CapabilityKind = "logs"

    def resolve(self, config: PlatformSettingsRecord) -> tuple[CapabilityStatus, str] | None:
        return (
            "active",
            "Structured JSON logs from every agent container, routed via the ECS log driver.",
        )


class _CloudWatchMetricsAdapter:
    adapter_id = "cloudwatch_metrics"
    capability: CapabilityKind = "metrics"

    def resolve(self, config: PlatformSettingsRecord) -> tuple[CapabilityStatus, str] | None:
        return "active", "Request, latency, and cost metrics emitted by the agent runtime."


class _XRayTracingAdapter:
    adapter_id = "aws_xray"
    capability: CapabilityKind = "distributed_tracing"

    def resolve(self, config: PlatformSettingsRecord) -> tuple[CapabilityStatus, str] | None:
        return "active", "Distributed traces across agent runtime, Lambda, and Step Functions."


# ── Customer-configured, optional (R46 — never a mandatory dependency) ─────


class _OpenTelemetryCollectorAdapter:
    adapter_id = "otel_collector"
    capability: CapabilityKind = "opentelemetry"

    def resolve(self, config: PlatformSettingsRecord) -> tuple[CapabilityStatus, str] | None:
        if not config.otel_endpoint:
            return "inactive", "No OpenTelemetry collector endpoint configured."
        return (
            "unknown",
            f"Spans are routed to the configured collector ({config.otel_endpoint}); "
            "reachability isn't actively health-probed.",
        )


class _LangfuseTracingAdapter:
    adapter_id = "langfuse"
    capability: CapabilityKind = "distributed_tracing"

    def resolve(self, config: PlatformSettingsRecord) -> tuple[CapabilityStatus, str] | None:
        if not config.langfuse_enabled:
            return None
        return (
            "unknown",
            "Also exported to your self-hosted Langfuse instance for LLM-specific tracing.",
        )


class _DatadogAdapter:
    adapter_id = "datadog"
    capability: CapabilityKind = "opentelemetry"

    def resolve(self, config: PlatformSettingsRecord) -> tuple[CapabilityStatus, str] | None:
        if not config.datadog_enabled:
            return None
        return "unknown", "Also routed to Datadog APM via your OTel collector."


class _GrafanaAdapter:
    adapter_id = "grafana"
    capability: CapabilityKind = "opentelemetry"

    def resolve(self, config: PlatformSettingsRecord) -> tuple[CapabilityStatus, str] | None:
        if not config.grafana_enabled:
            return None
        return "unknown", "Also routed to Grafana/Loki via your OTel collector."


class _NewRelicAdapter:
    adapter_id = "new_relic"
    capability: CapabilityKind = "opentelemetry"

    def resolve(self, config: PlatformSettingsRecord) -> tuple[CapabilityStatus, str] | None:
        if not config.new_relic_enabled:
            return None
        return "unknown", "Also routed to New Relic via your OTel collector."


class _DynatraceAdapter:
    adapter_id = "dynatrace"
    capability: CapabilityKind = "opentelemetry"

    def resolve(self, config: PlatformSettingsRecord) -> tuple[CapabilityStatus, str] | None:
        if not config.dynatrace_enabled:
            return None
        return "unknown", "Also routed to Dynatrace via your OTel collector."


_ADAPTERS: tuple[ObservabilityAdapter, ...] = (
    _CloudWatchLogsAdapter(),
    _CloudWatchMetricsAdapter(),
    _XRayTracingAdapter(),
    _OpenTelemetryCollectorAdapter(),
    _LangfuseTracingAdapter(),
    _DatadogAdapter(),
    _GrafanaAdapter(),
    _NewRelicAdapter(),
    _DynatraceAdapter(),
)


def discover_capabilities(config: PlatformSettingsRecord) -> CapabilityDiscoveryResponse:
    by_capability: dict[CapabilityKind, ObservabilityCapability] = {}

    for adapter in _ADAPTERS:
        result = adapter.resolve(config)
        if result is None:
            continue
        status, detail = result

        existing = by_capability.get(adapter.capability)
        if existing is None:
            by_capability[adapter.capability] = ObservabilityCapability(
                capability=adapter.capability,
                status=status,
                detail=detail,
                adapters=[adapter.adapter_id],
            )
            continue

        merged_status = (
            status if _STATUS_RANK[status] > _STATUS_RANK[existing.status] else existing.status
        )
        by_capability[adapter.capability] = existing.model_copy(
            update={
                "status": merged_status,
                "adapters": [*existing.adapters, adapter.adapter_id],
                "detail": f"{existing.detail} {detail}",
            }
        )

    return CapabilityDiscoveryResponse(
        capabilities=[by_capability[kind] for kind in _CAPABILITY_ORDER if kind in by_capability]
    )
