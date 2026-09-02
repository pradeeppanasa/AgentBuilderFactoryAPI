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


async def test_update_connector_changes_fields(make_user_and_token) -> None:
    _, token = await make_user_and_token(TENANT_A, role="developer")

    with TestClient(app) as client:
        created = client.post(
            "/api/v1/connectors",
            json={"name": "Doc Verify", "executor_type": "http", "description": "v1"},
            headers=_bearer(token),
        )
        connector_id = created.json()["connector_id"]

        updated = client.put(
            f"/api/v1/connectors/{connector_id}",
            json={
                "name": "Document Verification API",
                "executor_type": "http",
                "description": "v2 — verifies identity documents",
                "endpoint_template": "https://api.docverify.example.com/check",
                "credentials_required": ["api_key"],
            },
            headers=_bearer(token),
        )

    assert updated.status_code == 200
    body = updated.json()
    assert body["connector_id"] == connector_id  # id never changes on update
    assert body["name"] == "Document Verification API"
    assert body["description"] == "v2 — verifies identity documents"
    assert body["endpoint_template"] == "https://api.docverify.example.com/check"
    assert body["credentials_required"] == ["api_key"]


async def test_update_connector_404_for_other_tenant(make_user_and_token) -> None:
    _, token_a = await make_user_and_token(TENANT_A, role="developer")
    _, token_b = await make_user_and_token(TENANT_B, role="developer")

    with TestClient(app) as client:
        created = client.post(
            "/api/v1/connectors",
            json={"name": "X", "executor_type": "http", "description": "d"},
            headers=_bearer(token_a),
        )
        connector_id = created.json()["connector_id"]

        response = client.put(
            f"/api/v1/connectors/{connector_id}",
            json={"name": "Hijacked", "executor_type": "http", "description": "d"},
            headers=_bearer(token_b),
        )

    assert response.status_code == 404


async def test_update_connector_404_for_global_connector(make_user_and_token) -> None:
    """R01/is_global — a tenant must never be able to edit a Panasa-curated
    global connector (seed_global_connectors reapplies these at every
    startup; per-tenant edits would just be silently overwritten anyway,
    but the real point is a tenant has no business editing shared,
    Panasa-owned catalog entries at all)."""
    _, token = await make_user_and_token(TENANT_A, role="developer")

    with TestClient(app) as client:
        response = client.put(
            "/api/v1/connectors/jira",
            json={"name": "Hijacked Jira", "executor_type": "http", "description": "d"},
            headers=_bearer(token),
        )

    assert response.status_code == 404


async def test_update_connector_forbidden_for_auditor(make_user_and_token) -> None:
    _, token_dev = await make_user_and_token(TENANT_A, role="developer")
    _, token_auditor = await make_user_and_token(TENANT_A, role="auditor")

    with TestClient(app) as client:
        created = client.post(
            "/api/v1/connectors",
            json={"name": "X", "executor_type": "http", "description": "d"},
            headers=_bearer(token_dev),
        )
        connector_id = created.json()["connector_id"]

        response = client.put(
            f"/api/v1/connectors/{connector_id}",
            json={"name": "Y", "executor_type": "http", "description": "d"},
            headers=_bearer(token_auditor),
        )

    assert response.status_code == 403


async def test_delete_connector_removes_it(make_user_and_token) -> None:
    _, token = await make_user_and_token(TENANT_A, role="developer")

    with TestClient(app) as client:
        created = client.post(
            "/api/v1/connectors",
            json={"name": "Throwaway", "executor_type": "http", "description": "d"},
            headers=_bearer(token),
        )
        connector_id = created.json()["connector_id"]

        deleted = client.delete(f"/api/v1/connectors/{connector_id}", headers=_bearer(token))
        assert deleted.status_code == 204

        gone = client.get(f"/api/v1/connectors/{connector_id}", headers=_bearer(token))

    assert gone.status_code == 404


async def test_delete_connector_404_for_other_tenant(make_user_and_token) -> None:
    _, token_a = await make_user_and_token(TENANT_A, role="developer")
    _, token_b = await make_user_and_token(TENANT_B, role="developer")

    with TestClient(app) as client:
        created = client.post(
            "/api/v1/connectors",
            json={"name": "X", "executor_type": "http", "description": "d"},
            headers=_bearer(token_a),
        )
        connector_id = created.json()["connector_id"]

        response = client.delete(f"/api/v1/connectors/{connector_id}", headers=_bearer(token_b))

    assert response.status_code == 404


async def test_delete_connector_404_for_global_connector(make_user_and_token) -> None:
    _, token = await make_user_and_token(TENANT_A, role="developer")

    with TestClient(app) as client:
        response = client.delete("/api/v1/connectors/companies-house", headers=_bearer(token))

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
