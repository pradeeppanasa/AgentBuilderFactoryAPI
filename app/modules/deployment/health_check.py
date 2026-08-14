"""HEALTH_CHECK stage (CLAUDE.md Section 6.2 / F1 / Phase 13):
"POST /health on the new agent endpoint, verifies 200 + correct version."

Deliberately POST, not GET, per the spec's own wording (Section 6.2 and F1
both say it explicitly) — unusual for a health check, but not this
project's call to silently "fix". `expected_version` is compared against a
`"version"` field in the response body; a generated agent runtime that
doesn't echo one back simply can't have its version verified (treated as a
mismatch, not skipped — "verifies... correct version" is not optional).

R15: never logs/returns the response body itself, only status code and a
short summary — a generated agent's /health response is that agent's
business surface, not something this Runtime should assume is safe to
persist verbatim.
"""

from __future__ import annotations

import httpx
from pydantic import BaseModel


class HealthCheckResult(BaseModel):
    passed: bool
    status_code: int | None = None
    summary: str


class HealthChecker:
    def __init__(
        self, transport: httpx.AsyncBaseTransport | None = None, timeout_seconds: float = 10.0
    ) -> None:
        self._transport = transport
        self._timeout = timeout_seconds

    async def check(self, health_check_url: str, expected_version: str) -> HealthCheckResult:
        async with httpx.AsyncClient(transport=self._transport, timeout=self._timeout) as client:
            try:
                response = await client.post(health_check_url)
            except httpx.HTTPError as exc:
                return HealthCheckResult(
                    passed=False,
                    summary=f"POST {health_check_url} failed: {type(exc).__name__}",
                )

        if response.status_code != 200:
            return HealthCheckResult(
                passed=False,
                status_code=response.status_code,
                summary=f"POST {health_check_url} -> {response.status_code}",
            )

        try:
            body = response.json()
        except ValueError:
            return HealthCheckResult(
                passed=False,
                status_code=200,
                summary="200 but response body was not JSON — cannot verify version",
            )

        actual_version = body.get("version")
        if str(actual_version) != str(expected_version):
            return HealthCheckResult(
                passed=False,
                status_code=200,
                summary=f"Version mismatch: expected {expected_version!r}, got {actual_version!r}",
            )

        return HealthCheckResult(
            passed=True,
            status_code=200,
            summary=f"POST {health_check_url} -> 200, version {expected_version} confirmed",
        )
