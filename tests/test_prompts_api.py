"""API tests for the Prompt Library (Priority 2 nav addition)."""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.main import app

TENANT_A = "tenant-a"
TENANT_B = "tenant-b"


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def test_create_list_get_prompt(make_user_and_token) -> None:
    _, token = await make_user_and_token(TENANT_A, role="developer")

    with TestClient(app) as client:
        created = client.post(
            "/api/v1/platform/prompts",
            json={
                "name": "KYC Verification Prompt",
                "content": "You are a KYC verification agent for {{company_name}}.",
                "tags": ["kyc", "compliance"],
            },
            headers=_bearer(token),
        )
        assert created.status_code == 201
        body = created.json()
        assert body["name"] == "KYC Verification Prompt"
        assert body["tags"] == ["kyc", "compliance"]
        prompt_id = body["prompt_id"]

        listed = client.get("/api/v1/platform/prompts", headers=_bearer(token))
        assert listed.status_code == 200
        assert any(item["prompt_id"] == prompt_id for item in listed.json()["items"])

        fetched = client.get(f"/api/v1/platform/prompts/{prompt_id}", headers=_bearer(token))
        assert fetched.status_code == 200
        assert fetched.json()["content"] == body["content"]


async def test_update_and_delete_prompt(make_user_and_token) -> None:
    _, token = await make_user_and_token(TENANT_A, role="developer")

    with TestClient(app) as client:
        created = client.post(
            "/api/v1/platform/prompts",
            json={"name": "Draft Prompt", "content": "Original content."},
            headers=_bearer(token),
        ).json()
        prompt_id = created["prompt_id"]

        updated = client.put(
            f"/api/v1/platform/prompts/{prompt_id}",
            json={"content": "Revised content."},
            headers=_bearer(token),
        )
        assert updated.status_code == 200
        assert updated.json()["content"] == "Revised content."
        assert updated.json()["name"] == "Draft Prompt"  # unchanged

        deleted = client.delete(f"/api/v1/platform/prompts/{prompt_id}", headers=_bearer(token))
        assert deleted.status_code == 204

        fetched = client.get(f"/api/v1/platform/prompts/{prompt_id}", headers=_bearer(token))
        assert fetched.status_code == 404


async def test_prompts_are_tenant_scoped(make_user_and_token) -> None:
    _, token_a = await make_user_and_token(TENANT_A, role="developer")
    _, token_b = await make_user_and_token(TENANT_B, role="developer")

    with TestClient(app) as client:
        created = client.post(
            "/api/v1/platform/prompts",
            json={"name": "Tenant A Prompt", "content": "..."},
            headers=_bearer(token_a),
        ).json()

        listed_b = client.get("/api/v1/platform/prompts", headers=_bearer(token_b))
        assert all(item["prompt_id"] != created["prompt_id"] for item in listed_b.json()["items"])

        fetched_b = client.get(
            f"/api/v1/platform/prompts/{created['prompt_id']}", headers=_bearer(token_b)
        )
        assert fetched_b.status_code == 404


async def test_auditor_cannot_create_prompt(make_user_and_token) -> None:
    _, token = await make_user_and_token(TENANT_A, role="auditor")

    with TestClient(app) as client:
        response = client.post(
            "/api/v1/platform/prompts",
            json={"name": "x", "content": "y"},
            headers=_bearer(token),
        )

    assert response.status_code == 403


async def test_auditor_can_list_prompts(make_user_and_token) -> None:
    _, token = await make_user_and_token(TENANT_A, role="auditor")

    with TestClient(app) as client:
        response = client.get("/api/v1/platform/prompts", headers=_bearer(token))

    assert response.status_code == 200
