"""Connector dry-run tester (Section 5.4: POST /connectors/{id}/test).

http-executor connectors only for now — lambda/sql/mcp dry-runs would each
need their own invocation path (direct Lambda invoke, a read-only query
runner, an MCP client) that nothing in this codebase builds yet; this
returns a clear "not supported" result for those rather than pretending to
test them.

Nothing here is persisted, logged, or echoed back: `credentials` exists
only for the duration of this one call (Section 11 rules 1/2 apply just as
much to a value that arrives via API request body as to one already in
Secrets Manager).
"""

from __future__ import annotations

from typing import Any

import httpx
from pydantic import BaseModel

from app.modules.connectors.models import ConnectorRecord


class ConnectorTestResult(BaseModel):
    success: bool
    status_code: int | None = None
    summary: str


class ConnectorTester:
    def __init__(self, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._transport = transport

    async def test(
        self,
        connector: ConnectorRecord,
        endpoint_params: dict[str, str] | None = None,
        credentials: dict[str, str] | None = None,
        test_payload: dict[str, Any] | None = None,
    ) -> ConnectorTestResult:
        if connector.executor_type != "http":
            return ConnectorTestResult(
                success=False,
                summary=(
                    "Dry-run test not yet supported for "
                    f"executor_type={connector.executor_type!r}"
                ),
            )

        if not connector.endpoint_template:
            return ConnectorTestResult(
                success=False, summary="Connector has no endpoint_template configured"
            )

        try:
            url = connector.endpoint_template.format(**(endpoint_params or {}))
        except KeyError as exc:
            return ConnectorTestResult(success=False, summary=f"Missing endpoint parameter: {exc}")

        headers = _auth_headers(credentials or {})
        method = "GET" if test_payload is None else "POST"

        async with httpx.AsyncClient(transport=self._transport, timeout=10.0) as client:
            try:
                response = await client.request(method, url, headers=headers, json=test_payload)
            except httpx.HTTPError as exc:
                return ConnectorTestResult(
                    success=False, summary=f"{method} {url} failed: {type(exc).__name__}"
                )

        return ConnectorTestResult(
            success=response.is_success,
            status_code=response.status_code,
            summary=f"{method} {url} -> {response.status_code}",
        )


def _auth_headers(credentials: dict[str, str]) -> dict[str, str]:
    """Generic best-effort auth header — real per-connector auth schemes
    (OAuth bearer vs. API key header vs. basic auth, ...) are a tool-
    execution-time concern (Section 21/ToolConfig.connection_id), out of
    scope for this dry-run helper."""
    if "api_key" in credentials:
        return {"Authorization": f"Bearer {credentials['api_key']}"}
    if "access_token" in credentials:
        return {"Authorization": f"Bearer {credentials['access_token']}"}
    return {}
