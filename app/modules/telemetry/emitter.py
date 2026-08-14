"""Telemetry sanitiser + emitter (CLAUDE.md Section 12 / A7 / R30 — Phase 16).

Three amendments in CLAUDE.md define the allowed/blocked field lists at
increasing levels of detail (Section 12's original two sets, A7's
"formalised" ALLOWED_TELEMETRY_FIELDS/BLOCKED_FROM_TELEMETRY, and Section
32.5's R30, the newest). None of them contradict each other — each later
one is a superset of the concern the last one raised — so this module
unions all three rather than picking one and dropping the others' coverage.

Default-deny, not "allow unless blocked": a field must be in
ALLOWED_TELEMETRY_FIELDS to ever leave the sanitiser. BLOCKED_TELEMETRY_FIELDS
exists only so a caller mistake (someone passes "system_prompt" into the
payload dict) gets logged loudly instead of silently vanishing the way an
unrecognised-but-harmless key would. The module-level assertion below is a
static self-check that the two sets never overlap — if it ever fired it
would mean this file itself was edited incorrectly, not that a caller did
something wrong.

TELEMETRY_ENABLED defaults to False (R16) and gates emission entirely; each
category can additionally be toggled independently via
PUT /api/v1/platform/telemetry-config (app/api/v1/platform.py) — a category
being off filters its fields out even while the master switch is on.
"""

from __future__ import annotations

from typing import Any, Literal

import httpx
from pydantic import BaseModel, Field

from app.config import Settings
from app.shared.logging import get_logger

log = get_logger()

TelemetryCategory = Literal["usage", "performance", "cost", "errors"]

# Every field ever named across Section 12, A7, and R30 as safe operational
# metadata, grouped so PUT /platform/telemetry-config can toggle each group
# independently. A field appears in exactly one category.
_CATEGORY_FIELDS: dict[TelemetryCategory, frozenset[str]] = {
    "usage": frozenset(
        {
            "agent_id",
            "deployment_id",
            "deployment_status",
            "version",
            "request_id",
            "trace_id",
            "model_id",
            "model_provider",
            "platform_version",
            "runtime_version",
            "status",
        }
    ),
    "performance": frozenset(
        {
            "latency_ms",
            "p95_latency_ms",
            "p99_latency_ms",
            "throughput_rps",
        }
    ),
    "cost": frozenset(
        {
            "request_count",
            "input_token_count",
            "output_token_count",
            "total_token_count",
            "estimated_cost_usd",
            "tool_execution_ms",
        }
    ),
    "errors": frozenset(
        {
            "error_count",
            "error_code",
            "error_category",
        }
    ),
}

ALLOWED_TELEMETRY_FIELDS: frozenset[str] = frozenset().union(*_CATEGORY_FIELDS.values())

# R30 + A7 + Section 12, unioned. Never sent regardless of category toggles.
BLOCKED_TELEMETRY_FIELDS: frozenset[str] = frozenset(
    {
        # Conversation content
        "prompt",
        "response",
        "system_prompt",
        "conversation_history",
        "user_message",
        "assistant_message",
        "session_history",
        "transcript",
        # Tool / skill I/O
        "tool_arguments",
        "tool_response",
        "skill_input",
        "skill_output",
        # Knowledge base / RAG content
        "kb_content",
        "retrieved_chunks",
        "embeddings",
        "documents",
        # Memory
        "memory_content",
        "persistent_memory",
        # PII / customer identity
        "pii",
        "user_id_hash",
        "user_data",
        "name",
        "email",
        "phone",
        # Infrastructure secrets
        "secret_arn",
        "access_token",
        "terraform_state",
        "security_findings_detail",
        "iam_policy",
    }
)

assert not (
    ALLOWED_TELEMETRY_FIELDS & BLOCKED_TELEMETRY_FIELDS
), "ALLOWED_TELEMETRY_FIELDS and BLOCKED_TELEMETRY_FIELDS must be disjoint"


class TelemetryCategoryToggles(BaseModel):
    usage: bool = True
    performance: bool = True
    cost: bool = True
    errors: bool = True


class TelemetryConfig(BaseModel):
    """Mutated in place by PUT /platform/telemetry-config — TelemetrySanitiser
    and TelemetryEmitter both hold a reference to the same instance
    (app.state.telemetry_config), so an admin's change takes effect on the
    very next emit() with no re-wiring needed."""

    enabled: bool = False
    categories: TelemetryCategoryToggles = Field(default_factory=TelemetryCategoryToggles)


class TelemetrySanitiser:
    def __init__(self, config: TelemetryConfig) -> None:
        self._config = config

    def sanitise(self, raw_event: dict[str, Any]) -> dict[str, Any]:
        enabled_fields: set[str] = set()
        toggles = self._config.categories
        for category, fields in _CATEGORY_FIELDS.items():
            if getattr(toggles, category):
                enabled_fields |= fields

        sanitised: dict[str, Any] = {}
        for key, value in raw_event.items():
            if key in enabled_fields:
                sanitised[key] = value
            elif key in BLOCKED_TELEMETRY_FIELDS:
                log.warning("telemetry.field.blocked", field=key)
            # else: either an allowed field whose category is currently
            # disabled, or an unrecognised key — both silently dropped,
            # since neither is a caller mistake worth warning about.
        return sanitised


class TelemetryEmitter:
    """Fire-and-forget, fail-open — matches
    app.modules.observability.metrics.MetricsEmitter's posture: a telemetry
    outage must never fail the operation that triggered the event."""

    def __init__(self, settings: Settings, config: TelemetryConfig) -> None:
        self._settings = settings
        self._config = config
        self._sanitiser = TelemetrySanitiser(config)

    async def emit(self, raw_event: dict[str, Any]) -> None:
        if not self._config.enabled:
            return
        if not self._settings.telemetry_endpoint:
            return

        payload = self._sanitiser.sanitise(raw_event)
        if not payload:
            return

        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                await client.post(self._settings.telemetry_endpoint, json=payload)
        except Exception:
            log.warning("telemetry.emit.failed", exc_info=True)
