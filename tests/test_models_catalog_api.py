"""GET /api/v1/platform/models (CLAUDE.md Section 32.1, R37).

Backend-authoritative model catalog — no auth required, matching /health's
existing convention for platform-level, non-tenant-scoped endpoints.
"""

from fastapi.testclient import TestClient

from app.main import app
from app.services.model_router import PROVIDER_PREFIX


def test_list_models_returns_catalog() -> None:
    with TestClient(app) as client:
        response = client.get("/api/v1/platform/models")

    assert response.status_code == 200
    body = response.json()
    assert body["models"]
    for model in body["models"]:
        assert model["model_provider"] in PROVIDER_PREFIX
        assert model["model_id"]
        assert model["display_name"]
