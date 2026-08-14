"""GET/PUT /platform/telemetry-config (Phase 16) — end-to-end through the
real app. GET is unauthenticated (same posture as /version /health /models);
PUT is admin-only.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.main import app

TENANT_A = "tenant-a"


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def test_get_telemetry_config_defaults_disabled_with_no_auth() -> None:
    with TestClient(app) as client:
        response = client.get("/api/v1/platform/telemetry-config")

    assert response.status_code == 200
    body = response.json()
    assert body["enabled"] is False
    assert body["categories"] == {
        "usage": True,
        "performance": True,
        "cost": True,
        "errors": True,
    }


async def test_put_telemetry_config_forbidden_for_non_admin(make_user_and_token) -> None:
    _, developer_token = await make_user_and_token(TENANT_A, role="developer")

    with TestClient(app) as client:
        response = client.put(
            "/api/v1/platform/telemetry-config",
            json={"enabled": True},
            headers=_bearer(developer_token),
        )

    assert response.status_code == 403


async def test_put_telemetry_config_enables_master_switch(make_user_and_token) -> None:
    _, admin_token = await make_user_and_token(TENANT_A, role="admin")

    with TestClient(app) as client:
        response = client.put(
            "/api/v1/platform/telemetry-config",
            json={"enabled": True},
            headers=_bearer(admin_token),
        )

        assert response.status_code == 200
        assert response.json()["enabled"] is True

        # Persisted on the shared app.state object, not just echoed back.
        get_response = client.get("/api/v1/platform/telemetry-config")

    assert get_response.json()["enabled"] is True


async def test_put_telemetry_config_updates_categories_independently(
    make_user_and_token,
) -> None:
    _, admin_token = await make_user_and_token(TENANT_A, role="admin")

    with TestClient(app) as client:
        response = client.put(
            "/api/v1/platform/telemetry-config",
            json={
                "enabled": True,
                "categories": {
                    "usage": True,
                    "performance": True,
                    "cost": False,
                    "errors": False,
                },
            },
            headers=_bearer(admin_token),
        )

    assert response.status_code == 200
    body = response.json()
    assert body["enabled"] is True
    assert body["categories"] == {
        "usage": True,
        "performance": True,
        "cost": False,
        "errors": False,
    }


async def test_put_telemetry_config_omitted_fields_leave_existing_value(
    make_user_and_token,
) -> None:
    _, admin_token = await make_user_and_token(TENANT_A, role="admin")

    with TestClient(app) as client:
        client.put(
            "/api/v1/platform/telemetry-config",
            json={"enabled": True},
            headers=_bearer(admin_token),
        )

        # No "enabled" key this time — must stay True, not reset to default.
        response = client.put(
            "/api/v1/platform/telemetry-config",
            json={
                "categories": {"usage": True, "performance": True, "cost": True, "errors": False}
            },
            headers=_bearer(admin_token),
        )

    assert response.status_code == 200
    body = response.json()
    assert body["enabled"] is True
    assert body["categories"]["errors"] is False


async def test_put_telemetry_config_change_takes_effect_on_shared_emitter(
    make_user_and_token,
) -> None:
    """The config object the PUT route mutates is the exact same instance
    app.state.telemetry_emitter holds a reference to — this is the
    integration point emitter.py's docstring promises ("no re-wiring
    needed"), verified here rather than just asserted in a comment."""
    _, admin_token = await make_user_and_token(TENANT_A, role="admin")

    with TestClient(app) as client:
        client.put(
            "/api/v1/platform/telemetry-config",
            json={"enabled": True},
            headers=_bearer(admin_token),
        )

        emitter = app.state.telemetry_emitter
        assert emitter._config.enabled is True  # noqa: SLF001 — verifying shared-reference wiring

        client.put(
            "/api/v1/platform/telemetry-config",
            json={"enabled": False},
            headers=_bearer(admin_token),
        )

    assert emitter._config.enabled is False  # noqa: SLF001
