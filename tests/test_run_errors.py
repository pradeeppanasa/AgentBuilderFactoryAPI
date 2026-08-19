"""Unit tests for app.modules.runs.errors.map_error (Phase 2, Section 6)."""

from __future__ import annotations

from app.modules.runs.errors import map_error


def test_known_error_codes_map_to_business_language() -> None:
    reason, action = map_error("UnrecognizedClientException")
    assert "credentials" in reason.lower()
    assert action

    reason, action = map_error("ThrottlingException")
    assert "rate limit" in reason.lower()
    assert action


def test_unknown_error_code_gets_generic_fallback() -> None:
    reason, action = map_error("SomeBrandNewExceptionNobodyHasSeenYet")
    assert reason == "An unexpected error occurred."
    assert "error ID" in action
