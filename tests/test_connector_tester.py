"""Unit tests for app.modules.connectors.tester (Section 5.4 dry-run test)."""

from __future__ import annotations

import httpx

from app.modules.connectors.models import ConnectorRecord
from app.modules.connectors.tester import ConnectorTester


def _connector(**overrides: object) -> ConnectorRecord:
    data: dict[str, object] = {
        "tenant_id": "GLOBAL",
        "connector_id": "jira",
        "name": "Jira",
        "executor_type": "http",
        "description": "desc",
        "endpoint_template": "https://{domain}.atlassian.net/rest/api/3",
        "credentials_required": ["api_key", "domain"],
        "is_global": True,
        "created_by": "panasa",
        "created_at": "2026-01-01T00:00:00+00:00",
    }
    data.update(overrides)
    return ConnectorRecord(**data)  # type: ignore[arg-type]


async def test_non_http_executor_is_not_supported() -> None:
    tester = ConnectorTester()
    result = await tester.test(_connector(executor_type="lambda"))
    assert result.success is False
    assert "not yet supported" in result.summary


async def test_missing_endpoint_template_fails_cleanly() -> None:
    tester = ConnectorTester()
    result = await tester.test(_connector(endpoint_template=None))
    assert result.success is False
    assert "endpoint_template" in result.summary


async def test_missing_endpoint_param_fails_cleanly() -> None:
    tester = ConnectorTester()
    result = await tester.test(_connector(), endpoint_params={})  # missing {domain}
    assert result.success is False
    assert "Missing endpoint parameter" in result.summary


async def test_successful_get_request() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "acme.atlassian.net"
        assert request.headers.get("Authorization") == "Bearer secret-key"
        return httpx.Response(200, json={"ok": True})

    tester = ConnectorTester(transport=httpx.MockTransport(handler))
    result = await tester.test(
        _connector(),
        endpoint_params={"domain": "acme"},
        credentials={"api_key": "secret-key"},
    )
    assert result.success is True
    assert result.status_code == 200


async def test_failed_status_code_is_not_success() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": "unauthorized"})

    tester = ConnectorTester(transport=httpx.MockTransport(handler))
    result = await tester.test(_connector(), endpoint_params={"domain": "acme"})
    assert result.success is False
    assert result.status_code == 401


async def test_test_payload_switches_to_post() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        return httpx.Response(200)

    tester = ConnectorTester(transport=httpx.MockTransport(handler))
    result = await tester.test(
        _connector(), endpoint_params={"domain": "acme"}, test_payload={"foo": "bar"}
    )
    assert result.success is True


async def test_access_token_credential_is_used_when_no_api_key() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers.get("Authorization") == "Bearer oauth-token"
        return httpx.Response(200)

    tester = ConnectorTester(transport=httpx.MockTransport(handler))
    result = await tester.test(
        _connector(),
        endpoint_params={"domain": "acme"},
        credentials={"access_token": "oauth-token"},
    )
    assert result.success is True


async def test_no_credentials_sends_no_auth_header() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert "Authorization" not in request.headers
        return httpx.Response(200)

    tester = ConnectorTester(transport=httpx.MockTransport(handler))
    result = await tester.test(_connector(), endpoint_params={"domain": "acme"})
    assert result.success is True
