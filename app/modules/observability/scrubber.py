"""Span attribute scrubber (CLAUDE.md Section 40 — R30/R45).

Every custom attribute attached to an X-Ray segment/subsegment (and any
future OpenTelemetry span, per R45-4/R45-5's eventual OTLP exporter) must
pass through `safe_span_attributes()` first. `safe_put_metadata()` is the
one sanctioned way to attach custom key/value data to the current trace in
this codebase — any future instrumentation should call it rather than
`xray_recorder.put_metadata` directly, so a forgotten scrub can never leak
customer data into a trace.

Note on today's actual call sites: `XRayMiddleware` (app/middleware/xray.py)
only calls `put_http_meta` with a fixed schema (url, method, status) — those
are already on the "always allowed" list (Section 40.2) and aren't
arbitrary key/value pairs, so there is nothing to retrofit there. This
module exists so that the moment any code needs to attach a custom
attribute (agent_id, stage_name, tool_name, etc.) to a span, the safe path
already exists and is the obvious thing to reach for.
"""

from __future__ import annotations

from typing import Any

from aws_xray_sdk.core import xray_recorder

from app.shared.logging import get_logger

log = get_logger()

BLOCKED_SPAN_KEYS = {
    "prompt",
    "message",
    "response",
    "content",
    "text",
    "tool_input",
    "tool_output",
    "tool_payload",
    "chunk",
    "document",
    "memory",
    "transcript",
    "api_key",
    "secret",
    "credential",
    "token",
    "authorization",
}

# Section 40.2's "Always allowed" list — checked BEFORE the blocked-substring
# scan below. Without this, the generic "token" blocked term (needed to
# catch access_token/refresh_token/auth_token/a bare "token" key) would also
# strip the explicitly-allowed token_count_input/token_count_output metrics,
# since "token_count_input" contains the substring "token".
ALLOWED_SPAN_KEYS = {
    "agent_id",
    "tenant_id",
    "project_id",
    "stage_name",
    "pipeline_step",
    "status_code",
    "token_count_input",
    "token_count_output",
    "duration_ms",
    "model_id",
    "tool_name",
    "kb_id",
}


def safe_span_attributes(attrs: dict[str, Any]) -> dict[str, Any]:
    """Strip any attribute whose key contains a blocked term (R30 + R45).

    Substring match, case-insensitive — catches `authorization_header`,
    `refresh_token`, `tool_output_summary` etc., not just exact matches.
    Section 40.2's explicitly allowed keys are exempted from the substring
    scan so a coincidental match (e.g. "token_count_input" containing
    "token") can't strip a metric that's supposed to always be allowed.
    """
    return {
        k: v
        for k, v in attrs.items()
        if k in ALLOWED_SPAN_KEYS or not any(blocked in k.lower() for blocked in BLOCKED_SPAN_KEYS)
    }


def safe_put_metadata(key: str, attrs: dict[str, Any], namespace: str = "default") -> None:
    """Scrub then attach metadata to the current X-Ray segment/subsegment.

    Fails open like `MetricsEmitter.emit` (app/modules/observability/
    metrics.py) — a missing/closed trace segment or an X-Ray SDK error must
    never break the request it would have annotated.
    """
    try:
        xray_recorder.put_metadata(key, safe_span_attributes(attrs), namespace)
    except Exception:
        log.warning("observability.span_metadata.failed", key=key, exc_info=True)
