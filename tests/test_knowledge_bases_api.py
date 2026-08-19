"""API tests for /api/v1/platform/knowledge-bases (CLAUDE_Advanced_Config.md
Section 4.1 / 5, CLAUDE.md Section 37.10/37.11)."""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.main import app

TENANT_A = "tenant-a"
TENANT_B = "tenant-b"


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def test_create_and_get_knowledge_base(make_user_and_token) -> None:
    _, token = await make_user_and_token(TENANT_A, role="developer")

    with TestClient(app) as client:
        created = client.post(
            "/api/v1/platform/knowledge-bases",
            json={
                "name": "Customer KB",
                "description": "Support docs",
                "source_type": "s3",
                "source_config": {"bucket": "docs-bucket"},
            },
            headers=_bearer(token),
        )
        assert created.status_code == 201
        body = created.json()
        assert body["status"] == "INDEXING"
        assert body["document_count"] == 0
        kb_id = body["kb_id"]

        fetched = client.get(f"/api/v1/platform/knowledge-bases/{kb_id}", headers=_bearer(token))
        assert fetched.status_code == 200
        assert fetched.json()["name"] == "Customer KB"


async def test_knowledge_base_is_tenant_scoped(make_user_and_token) -> None:
    _, token_a = await make_user_and_token(TENANT_A, role="developer")
    _, token_b = await make_user_and_token(TENANT_B, role="developer")

    with TestClient(app) as client:
        created = client.post(
            "/api/v1/platform/knowledge-bases",
            json={"name": "Private KB", "description": "d", "source_type": "manual"},
            headers=_bearer(token_a),
        )
        kb_id = created.json()["kb_id"]

        hidden = client.get(f"/api/v1/platform/knowledge-bases/{kb_id}", headers=_bearer(token_b))
        assert hidden.status_code == 404

        listed_b = client.get("/api/v1/platform/knowledge-bases", headers=_bearer(token_b))
        assert kb_id not in {item["kb_id"] for item in listed_b.json()["items"]}


async def test_analyst_can_read_but_not_write(make_user_and_token) -> None:
    _, analyst_token = await make_user_and_token(TENANT_A, role="analyst")

    with TestClient(app) as client:
        listed = client.get("/api/v1/platform/knowledge-bases", headers=_bearer(analyst_token))
        assert listed.status_code == 200

        denied = client.post(
            "/api/v1/platform/knowledge-bases",
            json={"name": "X", "description": "d", "source_type": "manual"},
            headers=_bearer(analyst_token),
        )
        assert denied.status_code == 403


async def test_get_unknown_kb_returns_404(make_user_and_token) -> None:
    _, token = await make_user_and_token(TENANT_A, role="developer")

    with TestClient(app) as client:
        response = client.get(
            "/api/v1/platform/knowledge-bases/does-not-exist", headers=_bearer(token)
        )
    assert response.status_code == 404


async def test_reindex_sets_status_back_to_indexing(make_user_and_token) -> None:
    _, token = await make_user_and_token(TENANT_A, role="developer")

    with TestClient(app) as client:
        created = client.post(
            "/api/v1/platform/knowledge-bases",
            json={"name": "Reindex Me", "description": "d", "source_type": "url"},
            headers=_bearer(token),
        )
        kb_id = created.json()["kb_id"]

        reindexed = client.post(
            f"/api/v1/platform/knowledge-bases/{kb_id}/reindex", headers=_bearer(token)
        )
        assert reindexed.status_code == 200
        assert reindexed.json()["status"] == "INDEXING"


async def test_delete_blocked_when_referenced_by_agent(make_user_and_token) -> None:
    _, token = await make_user_and_token(TENANT_A, role="developer")

    with TestClient(app) as client:
        kb = client.post(
            "/api/v1/platform/knowledge-bases",
            json={"name": "Referenced KB", "description": "d", "source_type": "manual"},
            headers=_bearer(token),
        )
        kb_id = kb.json()["kb_id"]

        agent = client.post(
            "/api/v1/agents",
            json={
                "name": "Agent Using KB",
                "description": "d",
                "business_purpose": "p",
                "agent_type": "standard",
                "configuration": {
                    "model_id": "anthropic.claude-3-5-sonnet-20241022-v2:0",
                    "model_provider": "bedrock",
                    "system_prompt": "You help.",
                    "kb_id": kb_id,
                },
            },
            headers=_bearer(token),
        )
        assert agent.status_code == 201

        blocked = client.delete(f"/api/v1/platform/knowledge-bases/{kb_id}", headers=_bearer(token))
        assert blocked.status_code == 409
        assert agent.json()["agent_id"] in blocked.json()["detail"]


async def test_delete_succeeds_when_unreferenced(make_user_and_token) -> None:
    _, token = await make_user_and_token(TENANT_A, role="developer")

    with TestClient(app) as client:
        kb = client.post(
            "/api/v1/platform/knowledge-bases",
            json={"name": "Unused KB", "description": "d", "source_type": "manual"},
            headers=_bearer(token),
        )
        kb_id = kb.json()["kb_id"]

        deleted = client.delete(f"/api/v1/platform/knowledge-bases/{kb_id}", headers=_bearer(token))
        assert deleted.status_code == 204

        fetched = client.get(f"/api/v1/platform/knowledge-bases/{kb_id}", headers=_bearer(token))
        assert fetched.status_code == 404
