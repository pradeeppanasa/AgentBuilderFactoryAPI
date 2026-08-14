"""API tests for /api/v1/connectors (CLAUDE.md Section 5.4).

The /test endpoint's dependency is overridden with a ConnectorTester built
on httpx.MockTransport — no real network call ever leaves this test suite
(same discipline as test_git_provider_github.py's _Recorder).
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from app.dependencies import get_connector_tester
from app.main import app
from app.modules.connectors.tester import ConnectorTester

TENANT_A = "tenant-a"
TENANT_B = "tenant-b"


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def test_list_connectors_includes_seeded_globals(make_user_and_token) -> None:
    _, token = await make_user_and_token(TENANT_A, role="developer")

    with TestClient(app) as client:
        response = client.get("/api/v1/connectors", headers=_bearer(token))

    assert response.status_code == 200
    ids = {c["connector_id"] for c in response.json()["items"]}
    assert {"jira", "salesforce", "companies-house"} <= ids


async def test_create_connector_is_tenant_scoped(make_user_and_token) -> None:
    _, token_a = await make_user_and_token(TENANT_A, role="developer")
    _, token_b = await make_user_and_token(TENANT_B, role="developer")

    with TestClient(app) as client:
        created = client.post(
            "/api/v1/connectors",
            json={
                "name": "Internal Tool",
                "executor_type": "http",
                "description": "desc",
            },
            headers=_bearer(token_a),
        )
        assert created.status_code == 201
        connector_id = created.json()["connector_id"]

        visible_to_a = client.get(f"/api/v1/connectors/{connector_id}", headers=_bearer(token_a))
        assert visible_to_a.status_code == 200

        hidden_from_b = client.get(f"/api/v1/connectors/{connector_id}", headers=_bearer(token_b))
        assert hidden_from_b.status_code == 404


async def test_create_connector_forbidden_for_auditor(make_user_and_token) -> None:
    _, token = await make_user_and_token(TENANT_A, role="auditor")

    with TestClient(app) as client:
        response = client.post(
            "/api/v1/connectors",
            json={"name": "X", "executor_type": "http", "description": "d"},
            headers=_bearer(token),
        )

    assert response.status_code == 403


async def test_get_connector_404_for_unknown_id(make_user_and_token) -> None:
    _, token = await make_user_and_token(TENANT_A, role="developer")

    with TestClient(app) as client:
        response = client.get("/api/v1/connectors/does-not-exist", headers=_bearer(token))

    assert response.status_code == 404


@pytest.fixture
def mock_connector_tester() -> Iterator[None]:
    def _handler(request: httpx.Request) -> httpx.Response:
        assert request.headers.get("Authorization") == "Bearer test-key"
        return httpx.Response(200, json={"ok": True})

    tester = ConnectorTester(transport=httpx.MockTransport(_handler))
    app.dependency_overrides[get_connector_tester] = lambda: tester
    try:
        yield
    finally:
        del app.dependency_overrides[get_connector_tester]


async def test_test_connector_dry_run_success(
    make_user_and_token: Any, mock_connector_tester: None
) -> None:
    _, token = await make_user_and_token(TENANT_A, role="developer")

    with TestClient(app) as client:
        response = client.post(
            "/api/v1/connectors/jira/test",
            json={
                "endpoint_params": {"domain": "acme"},
                "credentials": {"api_key": "test-key"},
            },
            headers=_bearer(token),
        )

    assert response.status_code == 200
    body = response.json()
    assert body["success"] is True
    assert body["status_code"] == 200
    assert "test-key" not in body["summary"]


async def test_test_connector_404_for_unknown_connector(
    make_user_and_token: Any, mock_connector_tester: None
) -> None:
    _, token = await make_user_and_token(TENANT_A, role="developer")

    with TestClient(app) as client:
        response = client.post(
            "/api/v1/connectors/does-not-exist/test", json={}, headers=_bearer(token)
        )

    assert response.status_code == 404
