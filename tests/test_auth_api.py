from fastapi.testclient import TestClient

from app.main import app

TENANT_A = "tenant-a"
PASSWORD = "TestPassword123!"


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def test_login_returns_token_pair(make_user_and_token) -> None:
    user, _ = await make_user_and_token(TENANT_A, role="developer", password=PASSWORD)

    with TestClient(app) as client:
        response = client.post(
            "/api/v1/auth/login", json={"email": user.email, "password": PASSWORD}
        )

    assert response.status_code == 200
    body = response.json()
    assert set(body) == {"access_token", "refresh_token", "expires_in"}
    assert body["expires_in"] > 0


async def test_login_with_wrong_password_returns_401(make_user_and_token) -> None:
    user, _ = await make_user_and_token(TENANT_A, role="developer", password=PASSWORD)

    with TestClient(app) as client:
        response = client.post(
            "/api/v1/auth/login", json={"email": user.email, "password": "wrong-password"}
        )

    assert response.status_code == 401


async def test_login_with_unknown_email_returns_401() -> None:
    with TestClient(app) as client:
        response = client.post(
            "/api/v1/auth/login", json={"email": "nobody@example.com", "password": "x"}
        )

    assert response.status_code == 401


async def test_inactive_user_cannot_login(make_user_and_token) -> None:
    user, _ = await make_user_and_token(
        TENANT_A, role="developer", password=PASSWORD, is_active=False
    )

    with TestClient(app) as client:
        response = client.post(
            "/api/v1/auth/login", json={"email": user.email, "password": PASSWORD}
        )

    assert response.status_code == 403


async def test_me_returns_current_user(make_user_and_token) -> None:
    user, token = await make_user_and_token(TENANT_A, role="analyst")

    with TestClient(app) as client:
        response = client.get("/api/v1/auth/me", headers=_bearer(token))

    assert response.status_code == 200
    body = response.json()
    assert body["email"] == user.email
    assert body["role"] == "analyst"
    assert body["tenant_id"] == TENANT_A


async def test_refresh_issues_new_access_token(make_user_and_token) -> None:
    user, _ = await make_user_and_token(TENANT_A, role="developer", password=PASSWORD)

    with TestClient(app) as client:
        login = client.post(
            "/api/v1/auth/login", json={"email": user.email, "password": PASSWORD}
        ).json()

        refreshed = client.post(
            "/api/v1/auth/refresh", json={"refresh_token": login["refresh_token"]}
        )
        assert refreshed.status_code == 200
        new_access_token = refreshed.json()["access_token"]

        me_response = client.get("/api/v1/auth/me", headers=_bearer(new_access_token))

    assert me_response.status_code == 200
    assert me_response.json()["email"] == user.email


async def test_access_token_rejected_by_refresh_endpoint(make_user_and_token) -> None:
    _, access_token = await make_user_and_token(TENANT_A, role="developer")

    with TestClient(app) as client:
        response = client.post("/api/v1/auth/refresh", json={"refresh_token": access_token})

    assert response.status_code == 401


async def test_refresh_token_rejected_by_protected_endpoint(make_user_and_token) -> None:
    user, _ = await make_user_and_token(TENANT_A, role="developer", password=PASSWORD)

    with TestClient(app) as client:
        login = client.post(
            "/api/v1/auth/login", json={"email": user.email, "password": PASSWORD}
        ).json()

        response = client.get("/api/v1/auth/me", headers=_bearer(login["refresh_token"]))

    assert response.status_code == 401
