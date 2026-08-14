from fastapi.testclient import TestClient

from app.main import app


def test_health_returns_200_and_correct_shape() -> None:
    with TestClient(app) as client:
        response = client.get("/api/v1/platform/health")

    assert response.status_code == 200

    body = response.json()
    assert body["status"] == "ok"
    assert body["version"] == "1.0.0"
    assert body["mode"] == "prototype"

    services = body["services"]
    assert set(services) == {"database", "cache", "storage", "model_router", "observability"}
    # database/storage are moto-backed (real reachability check) and must be
    # "ok"; REDIS_URL is deliberately unreachable in tests (see conftest.py),
    # so cache is deterministically "error" here — the "ok" cache path is
    # unit-tested directly against fakeredis in test_platform_health.py.
    assert services["database"] == "ok"
    assert services["storage"] == "ok"
    assert services["cache"] == "error"
    assert services["model_router"] == "ok"
    assert services["observability"] == "disabled"  # no LANGFUSE_HOST configured
