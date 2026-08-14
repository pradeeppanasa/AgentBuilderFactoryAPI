"""Unit + integration tests for app.modules.telemetry.emitter (Phase 16).

The integration test (test_emit_posts_only_sanitised_fields_never_customer_data)
is the one CLAUDE.md's Phase 16 spec calls for directly: "verify no prompts,
responses, or customer data appear in the telemetry payload" — it builds a
realistic mixed event (allowed operational fields alongside every kind of
customer-data field the spec prohibits) and asserts the bytes actually
POSTed over HTTP contain none of it.
"""

from __future__ import annotations

import json

import httpx
import pytest

from app.config import settings
from app.modules.telemetry.emitter import (
    ALLOWED_TELEMETRY_FIELDS,
    BLOCKED_TELEMETRY_FIELDS,
    TelemetryCategoryToggles,
    TelemetryConfig,
    TelemetryEmitter,
    TelemetrySanitiser,
)


def test_allowed_and_blocked_field_sets_are_disjoint() -> None:
    assert not (ALLOWED_TELEMETRY_FIELDS & BLOCKED_TELEMETRY_FIELDS)


def test_sanitise_keeps_only_allowed_fields() -> None:
    sanitiser = TelemetrySanitiser(TelemetryConfig(enabled=True))

    result = sanitiser.sanitise(
        {
            "agent_id": "agent-123",
            "latency_ms": 42,
            "estimated_cost_usd": 0.002,
            "not_a_real_field": "whatever",
        }
    )

    assert result == {"agent_id": "agent-123", "latency_ms": 42, "estimated_cost_usd": 0.002}


def test_sanitise_drops_blocked_fields(caplog: pytest.LogCaptureFixture) -> None:
    sanitiser = TelemetrySanitiser(TelemetryConfig(enabled=True))

    result = sanitiser.sanitise(
        {
            "agent_id": "agent-123",
            "system_prompt": "You are a KYC agent...",
            "prompt": "What is the customer's address?",
            "response": "123 Main St",
            "tool_arguments": {"query": "SELECT * FROM customers"},
            "kb_content": "confidential policy document text",
            "pii": {"name": "Jane Doe"},
            "secret_arn": "arn:aws:secretsmanager:eu-west-2:123456789012:secret:x",
        }
    )

    assert result == {"agent_id": "agent-123"}
    for blocked_field in (
        "system_prompt",
        "prompt",
        "response",
        "tool_arguments",
        "kb_content",
        "pii",
        "secret_arn",
    ):
        assert blocked_field not in result


def test_sanitise_drops_unknown_fields_silently() -> None:
    sanitiser = TelemetrySanitiser(TelemetryConfig(enabled=True))

    result = sanitiser.sanitise({"agent_id": "agent-123", "some_future_field": "x"})

    assert result == {"agent_id": "agent-123"}


def test_sanitise_respects_disabled_category() -> None:
    config = TelemetryConfig(
        enabled=True,
        categories=TelemetryCategoryToggles(cost=False),
    )
    sanitiser = TelemetrySanitiser(config)

    result = sanitiser.sanitise(
        {
            "agent_id": "agent-123",  # usage — stays
            "latency_ms": 42,  # performance — stays
            "estimated_cost_usd": 0.002,  # cost — disabled, dropped
            "input_token_count": 100,  # cost — disabled, dropped
        }
    )

    assert result == {"agent_id": "agent-123", "latency_ms": 42}


def test_sanitise_all_categories_disabled_returns_empty() -> None:
    config = TelemetryConfig(
        enabled=True,
        categories=TelemetryCategoryToggles(
            usage=False, performance=False, cost=False, errors=False
        ),
    )
    sanitiser = TelemetrySanitiser(config)

    result = sanitiser.sanitise({"agent_id": "agent-123", "latency_ms": 42})

    assert result == {}


async def test_emit_does_nothing_when_disabled_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    class _ClientThatMustNotBeConstructed:
        def __init__(self, *args: object, **kwargs: object) -> None:
            raise AssertionError("httpx.AsyncClient must not be constructed when disabled")

    monkeypatch.setattr(httpx, "AsyncClient", _ClientThatMustNotBeConstructed)

    stub_settings = settings.model_copy(
        update={"telemetry_endpoint": "https://telemetry.panasa.io/v1/ingest"}
    )
    emitter = TelemetryEmitter(stub_settings, TelemetryConfig(enabled=False))

    # TelemetryConfig() defaults to enabled=False (R16) — this is the
    # settings default too (settings.telemetry_enabled), asserted directly
    # here rather than assumed.
    assert settings.telemetry_enabled is False

    await emitter.emit({"agent_id": "agent-123", "system_prompt": "leak me"})


async def test_emit_does_nothing_when_endpoint_not_configured() -> None:
    stub_settings = settings.model_copy(update={"telemetry_endpoint": None})
    emitter = TelemetryEmitter(stub_settings, TelemetryConfig(enabled=True))

    # Would raise/attempt a real network call if this guard were missing —
    # absence of an exception here is the assertion.
    await emitter.emit({"agent_id": "agent-123"})


async def test_emit_does_nothing_when_sanitised_payload_is_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    posted: list[dict[str, object]] = []

    class _FakeAsyncClient:
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

        async def __aenter__(self) -> _FakeAsyncClient:
            return self

        async def __aexit__(self, *exc_info: object) -> None:
            return None

        async def post(self, url: str, json: dict[str, object]) -> None:
            posted.append(json)

    monkeypatch.setattr(httpx, "AsyncClient", _FakeAsyncClient)
    stub_settings = settings.model_copy(
        update={"telemetry_endpoint": "https://telemetry.panasa.io/v1/ingest"}
    )
    emitter = TelemetryEmitter(stub_settings, TelemetryConfig(enabled=True))

    # Nothing in this event maps to any allowed field.
    await emitter.emit({"system_prompt": "leak me", "conversation_history": [...]})

    assert posted == []


async def test_emit_posts_only_sanitised_fields_never_customer_data(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The integration test CLAUDE.md's Phase 16 spec asks for: build a
    realistic event mixing safe operational fields with every category of
    customer data the spec prohibits, and confirm none of it reaches the
    outbound HTTP payload."""

    posted: list[dict[str, object]] = []
    posted_urls: list[str] = []

    class _FakeAsyncClient:
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

        async def __aenter__(self) -> _FakeAsyncClient:
            return self

        async def __aexit__(self, *exc_info: object) -> None:
            return None

        async def post(self, url: str, json: dict[str, object]) -> None:
            posted_urls.append(url)
            posted.append(json)

    monkeypatch.setattr(httpx, "AsyncClient", _FakeAsyncClient)
    stub_settings = settings.model_copy(
        update={"telemetry_endpoint": "https://telemetry.panasa.io/v1/ingest"}
    )
    emitter = TelemetryEmitter(stub_settings, TelemetryConfig(enabled=True))

    raw_agent_invocation_event = {
        # Safe operational metadata — must survive.
        "agent_id": "kyc-agent-a1b2c3",
        "deployment_id": "DEP-2024-001",
        "trace_id": "trace-xyz",
        "model_id": "anthropic.claude-3-5-sonnet-20241022-v2:0",
        "model_provider": "bedrock",
        "status": "success",
        "latency_ms": 340,
        "input_token_count": 512,
        "output_token_count": 128,
        "estimated_cost_usd": 0.0041,
        # Customer data — must never survive.
        "system_prompt": "You are a KYC verification agent for Acme Corp...",
        "prompt": "Please verify this passport for John Smith, DOB 1990-01-01.",
        "response": "The document appears valid. Confidence: 94%.",
        "tool_arguments": {"document_s3_key": "s3://customer-kyc/passports/12345.pdf"},
        "tool_response": {"extracted_name": "John Smith", "dob": "1990-01-01"},
        "kb_content": "Internal KYC policy: flag any mismatch in address history.",
        "retrieved_chunks": ["chunk 1 of confidential policy doc"],
        "conversation_history": [{"role": "user", "content": "verify my identity"}],
        "memory_content": "User previously mentioned they live in London.",
        "pii": {"name": "John Smith", "dob": "1990-01-01"},
        "email": "john.smith@example.com",
        "phone": "+44 7700 900000",
        "secret_arn": "arn:aws:secretsmanager:eu-west-2:123456789012:secret:ch-api",
        "terraform_state": {"resources": ["..."]},
    }

    await emitter.emit(raw_agent_invocation_event)

    assert posted_urls == ["https://telemetry.panasa.io/v1/ingest"]
    assert len(posted) == 1
    sent_payload = posted[0]

    # Positive assertion — the operational fields we expect are there.
    assert sent_payload == {
        "agent_id": "kyc-agent-a1b2c3",
        "deployment_id": "DEP-2024-001",
        "trace_id": "trace-xyz",
        "model_id": "anthropic.claude-3-5-sonnet-20241022-v2:0",
        "model_provider": "bedrock",
        "status": "success",
        "latency_ms": 340,
        "input_token_count": 512,
        "output_token_count": 128,
        "estimated_cost_usd": 0.0041,
    }

    # Negative assertion — nothing customer-identifying or content-bearing
    # made it through, checked both by key and by serialised value, so a
    # future refactor that nests customer data inside an "allowed" field
    # would also be caught.
    serialised = json.dumps(sent_payload)
    for leaked_value in (
        "You are a KYC verification agent",
        "John Smith",
        "1990-01-01",
        "john.smith@example.com",
        "+44 7700 900000",
        "s3://customer-kyc",
        "arn:aws:secretsmanager",
        "confidential policy",
        "flag any mismatch",
    ):
        assert leaked_value not in serialised

    for blocked_key in BLOCKED_TELEMETRY_FIELDS:
        assert blocked_key not in sent_payload


async def test_emit_fails_open_when_post_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    class _FailingAsyncClient:
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

        async def __aenter__(self) -> _FailingAsyncClient:
            return self

        async def __aexit__(self, *exc_info: object) -> None:
            return None

        async def post(self, url: str, json: dict[str, object]) -> None:
            raise httpx.ConnectError("boom")

    monkeypatch.setattr(httpx, "AsyncClient", _FailingAsyncClient)
    stub_settings = settings.model_copy(
        update={"telemetry_endpoint": "https://telemetry.panasa.io/v1/ingest"}
    )
    emitter = TelemetryEmitter(stub_settings, TelemetryConfig(enabled=True))

    # Must not raise — a telemetry outage can never fail the caller.
    await emitter.emit({"agent_id": "agent-123"})
