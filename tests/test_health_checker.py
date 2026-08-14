"""Unit tests for app.modules.deployment.health_check (CLAUDE.md Section 6.2 /
F1 / Phase 13: "POST /health on new agent endpoint, verifies 200 + correct
version")."""

from __future__ import annotations

import httpx

from app.modules.deployment.health_check import HealthChecker

URL = "https://agent-42.customer.example/health"


async def test_post_200_with_matching_version_passes() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        return httpx.Response(200, json={"status": "ok", "version": "3"})

    checker = HealthChecker(transport=httpx.MockTransport(handler))
    result = await checker.check(URL, expected_version="3")

    assert result.passed is True
    assert result.status_code == 200


async def test_non_200_status_fails() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"status": "unhealthy"})

    checker = HealthChecker(transport=httpx.MockTransport(handler))
    result = await checker.check(URL, expected_version="3")

    assert result.passed is False
    assert result.status_code == 503


async def test_version_mismatch_fails_even_with_200() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"status": "ok", "version": "2"})

    checker = HealthChecker(transport=httpx.MockTransport(handler))
    result = await checker.check(URL, expected_version="3")

    assert result.passed is False
    assert "mismatch" in result.summary
    assert "2" in result.summary and "3" in result.summary


async def test_missing_version_field_is_treated_as_mismatch() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"status": "ok"})

    checker = HealthChecker(transport=httpx.MockTransport(handler))
    result = await checker.check(URL, expected_version="3")

    assert result.passed is False


async def test_non_json_body_fails_cleanly() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="not json")

    checker = HealthChecker(transport=httpx.MockTransport(handler))
    result = await checker.check(URL, expected_version="3")

    assert result.passed is False
    assert "not JSON" in result.summary


async def test_request_error_fails_cleanly() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    checker = HealthChecker(transport=httpx.MockTransport(handler))
    result = await checker.check(URL, expected_version="3")

    assert result.passed is False
    assert result.status_code is None
    assert "failed" in result.summary
