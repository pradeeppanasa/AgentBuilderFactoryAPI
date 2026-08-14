"""API tests for POST /api/v1/connectors/import-openapi (CLAUDE.md Section
37.11)."""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.main import app

TENANT_A = "tenant-a"

_DOCUMENT = {
    "openapi": "3.0.0",
    "info": {"title": "Minimal", "version": "1.0"},
    "servers": [{"url": "https://api.example.com"}],
    "paths": {
        "/widgets": {
            "get": {
                "operationId": "listWidgets",
                "summary": "List widgets",
                "responses": {
                    "200": {"content": {"application/json": {"schema": {"type": "array"}}}}
                },
            }
        }
    },
}


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def test_import_openapi_creates_one_connector_per_operation(make_user_and_token) -> None:
    _, token = await make_user_and_token(TENANT_A, role="developer")

    with TestClient(app) as client:
        response = client.post(
            "/api/v1/connectors/import-openapi",
            json={"schema_document": _DOCUMENT},
            headers=_bearer(token),
        )

    assert response.status_code == 201
    created = response.json()["created"]
    assert len(created) == 1
    assert created[0]["name"] == "listWidgets"
    assert created[0]["executor_type"] == "http"
    assert created[0]["endpoint_template"] == "https://api.example.com/widgets"

    with TestClient(app) as client:
        listed = client.get("/api/v1/connectors", headers=_bearer(token))
    assert created[0]["connector_id"] in {c["connector_id"] for c in listed.json()["items"]}


async def test_import_openapi_accepts_yaml_string(make_user_and_token) -> None:
    _, token = await make_user_and_token(TENANT_A, role="developer")
    yaml_doc = """
openapi: "3.0.0"
paths:
  /ping:
    get:
      operationId: ping
      responses:
        "200":
          content:
            application/json:
              schema:
                type: object
"""

    with TestClient(app) as client:
        response = client.post(
            "/api/v1/connectors/import-openapi",
            json={"schema_document": yaml_doc},
            headers=_bearer(token),
        )

    assert response.status_code == 201
    assert response.json()["created"][0]["name"] == "ping"


async def test_import_openapi_rejects_malformed_document(make_user_and_token) -> None:
    _, token = await make_user_and_token(TENANT_A, role="developer")

    with TestClient(app) as client:
        response = client.post(
            "/api/v1/connectors/import-openapi",
            json={"schema_document": {"openapi": "3.0.0"}},
            headers=_bearer(token),
        )

    assert response.status_code == 400


async def test_import_openapi_forbidden_for_auditor(make_user_and_token) -> None:
    _, token = await make_user_and_token(TENANT_A, role="auditor")

    with TestClient(app) as client:
        response = client.post(
            "/api/v1/connectors/import-openapi",
            json={"schema_document": _DOCUMENT},
            headers=_bearer(token),
        )

    assert response.status_code == 403
