"""Tests for app.modules.observability.scrubber (CLAUDE.md Section 40 —
R30/R45 span attribute scrubbing)."""

from __future__ import annotations

from typing import Any

from aws_xray_sdk.core import xray_recorder

from app.modules.observability.scrubber import safe_put_metadata, safe_span_attributes


def test_span_attributes_scrubbed() -> None:
    dirty = {
        "agent_id": "abc",
        "tenant_id": "t1",
        "prompt": "secret user query",
        "tool_output": "confidential data",
        "token_count_input": 42,
    }
    clean = safe_span_attributes(dirty)
    assert "prompt" not in clean
    assert "tool_output" not in clean
    assert clean["agent_id"] == "abc"
    assert clean["token_count_input"] == 42


def test_scrubber_matches_by_substring_case_insensitive() -> None:
    dirty = {
        "Authorization_Header": "Bearer xyz",
        "refresh_token": "rt-123",
        "tool_output_summary": "leaked data",
        "System_Prompt_Hash": "abc123",
        "stage_name": "SECURITY_SCANNING",
    }
    clean = safe_span_attributes(dirty)
    assert clean == {"stage_name": "SECURITY_SCANNING"}


def test_scrubber_preserves_allowed_attributes() -> None:
    allowed = {
        "agent_id": "agent-1",
        "tenant_id": "tenant-1",
        "project_id": "proj-1",
        "stage_name": "EVALUATING",
        "status_code": "PASS",
        "token_count_input": 10,
        "token_count_output": 20,
        "duration_ms": 123,
        "model_id": "anthropic.claude-3-5-haiku-20241022-v1:0",
        "tool_name": "companies-house-lookup",
        "kb_id": "kb-1",
    }
    assert safe_span_attributes(allowed) == allowed


def test_scrubber_blocks_every_documented_blocked_key() -> None:
    dirty = {
        "prompt": "x",
        "message": "x",
        "response": "x",
        "content": "x",
        "text": "x",
        "tool_input": "x",
        "tool_output": "x",
        "tool_payload": "x",
        "chunk": "x",
        "document": "x",
        "memory": "x",
        "transcript": "x",
        "api_key": "x",
        "secret": "x",
        "credential": "x",
        "token": "x",
    }
    assert safe_span_attributes(dirty) == {}


def test_safe_put_metadata_scrubs_before_recording() -> None:
    captured: dict[str, Any] = {}

    def _fake_put_metadata(key: str, value: Any, namespace: str = "default") -> None:
        captured["key"] = key
        captured["value"] = value
        captured["namespace"] = namespace

    with xray_recorder.in_segment("test-segment"):
        original = xray_recorder.put_metadata
        xray_recorder.put_metadata = _fake_put_metadata  # type: ignore[method-assign]
        try:
            safe_put_metadata(
                "attributes",
                {"agent_id": "abc", "prompt": "should not appear"},
            )
        finally:
            xray_recorder.put_metadata = original  # type: ignore[method-assign]

    assert captured["key"] == "attributes"
    assert captured["value"] == {"agent_id": "abc"}


def test_safe_put_metadata_fails_open_when_no_segment_open() -> None:
    """No open X-Ray segment must never raise into the caller."""
    safe_put_metadata("attributes", {"agent_id": "abc"})
