"""API tests for the Runs feature (Observability — Runs Feature, Phase 1).

There is no real Generated Agent Runtime in this environment, so
POST /agents/{id}/runs/seed-demo — gated behind settings.seed_runs_enabled,
default False — is the only way to exercise list/detail/filters end to end.
conftest.py forces the flag False by default; tests that need it on
monkeypatch app.config.settings directly (same pattern as other
settings-gated tests in this suite).
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.config import settings
from app.main import app

TENANT_A = "tenant-a"
TENANT_B = "tenant-b"


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _minimal_agent_payload(name: str = "Runs Test Agent") -> dict:
    return {
        "name": name,
        "description": "d",
        "business_purpose": "p",
        "agent_type": "standard",
        "configuration": {
            "model_id": "anthropic.claude-3-5-sonnet-20241022-v2:0",
            "model_provider": "bedrock",
            "system_prompt": "You are a test agent.",
        },
    }


async def test_seed_demo_forbidden_by_default(make_user_and_token) -> None:
    _, token = await make_user_and_token(TENANT_A, role="developer")

    with TestClient(app) as client:
        created = client.post(
            "/api/v1/agents", json=_minimal_agent_payload(), headers=_bearer(token)
        ).json()
        agent_id = created["agent_id"]

        response = client.post(
            f"/api/v1/agents/{agent_id}/runs/seed-demo", headers=_bearer(token)
        )

    assert response.status_code == 403


async def test_seed_demo_creates_all_four_states(
    make_user_and_token, monkeypatch
) -> None:
    monkeypatch.setattr(settings, "seed_runs_enabled", True)
    _, token = await make_user_and_token(TENANT_A, role="developer")

    with TestClient(app) as client:
        created = client.post(
            "/api/v1/agents", json=_minimal_agent_payload(), headers=_bearer(token)
        ).json()
        agent_id = created["agent_id"]

        seed_response = client.post(
            f"/api/v1/agents/{agent_id}/runs/seed-demo", headers=_bearer(token)
        )
        assert seed_response.status_code == 200
        seeded = seed_response.json()["items"]
        assert len(seeded) == 4
        statuses = {r["status"] for r in seeded}
        triggers = {r["trigger"] for r in seeded}
        assert statuses == {"SUCCESS", "FAILED", "RUNNING"}
        assert "SCHEDULER" in triggers
        running = next(r for r in seeded if r["status"] == "RUNNING")
        assert running["duration_ms"] is None
        assert running["cost_usd"] is None
        scheduled = next(r for r in seeded if r["trigger"] == "SCHEDULER")
        assert scheduled["schedule_expression"] == "cron(0 * * * ? *)"

        list_response = client.get(
            f"/api/v1/agents/{agent_id}/runs", headers=_bearer(token)
        )
        assert list_response.status_code == 200
        assert len(list_response.json()["items"]) == 4

        # R30/U-23 — every activity message is plain text, never a raw
        # dict/JSON blob passed straight through to the client.
        for run in list_response.json()["items"]:
            for event in run["activity"]:
                assert isinstance(event["message"], str)
                assert event["level"] in ("INFO", "WARNING", "ERROR", "DEBUG")


async def test_seed_demo_populates_execution_timeline_steps(
    make_user_and_token, monkeypatch
) -> None:
    """Phase 2, Section 5 — every seeded run has Execution Timeline steps,
    not just the flat Activity Feed. The SUCCESS run's steps' offsets +
    durations should roughly reconstruct its total duration_ms."""
    monkeypatch.setattr(settings, "seed_runs_enabled", True)
    _, token = await make_user_and_token(TENANT_A, role="developer")

    with TestClient(app) as client:
        created = client.post(
            "/api/v1/agents", json=_minimal_agent_payload(), headers=_bearer(token)
        ).json()
        agent_id = created["agent_id"]
        seeded = client.post(
            f"/api/v1/agents/{agent_id}/runs/seed-demo", headers=_bearer(token)
        ).json()["items"]

    success = next(r for r in seeded if r["status"] == "SUCCESS" and r["trigger"] == "API")
    assert len(success["steps"]) == 6
    llm_step = next(s for s in success["steps"] if s["component"] == "Amazon Bedrock")
    assert llm_step["model_id"] == "claude-3-5-sonnet-20241022"
    assert llm_step["input_tokens"] == 1245
    assert llm_step["output_tokens"] == 318
    assert llm_step["cost_usd"] == 0.014
    assert llm_step["error"] is None

    running = next(r for r in seeded if r["status"] == "RUNNING")
    running_step = next(s for s in running["steps"] if s["status"] == "RUNNING")
    assert running_step["duration_ms"] is None


async def test_seed_demo_failed_run_has_business_first_error(
    make_user_and_token, monkeypatch
) -> None:
    """Phase 2, Section 6 — a failed step's error is business-first: the
    raw AWS exception name is present but only inside the structured error
    object, never used as the top-level reason shown to the user."""
    monkeypatch.setattr(settings, "seed_runs_enabled", True)
    _, token = await make_user_and_token(TENANT_A, role="developer")

    with TestClient(app) as client:
        created = client.post(
            "/api/v1/agents", json=_minimal_agent_payload(), headers=_bearer(token)
        ).json()
        agent_id = created["agent_id"]
        seeded = client.post(
            f"/api/v1/agents/{agent_id}/runs/seed-demo", headers=_bearer(token)
        ).json()["items"]

    failed = next(r for r in seeded if r["status"] == "FAILED")
    failed_step = next(s for s in failed["steps"] if s["status"] == "FAILED")
    error = failed_step["error"]
    assert error is not None
    assert error["raw_error_code"] == "UnrecognizedClientException"
    assert error["business_reason"] == "AWS credentials are invalid or not configured."
    assert "IAM permissions" in error["recommended_action"]
    assert error["request_id"] == "req_abc123"
    assert error["trace_id"] == "tr_def456"
    assert error["region"] == "eu-west-1"


async def test_list_runs_filters_by_status(make_user_and_token, monkeypatch) -> None:
    monkeypatch.setattr(settings, "seed_runs_enabled", True)
    _, token = await make_user_and_token(TENANT_A, role="developer")

    with TestClient(app) as client:
        created = client.post(
            "/api/v1/agents", json=_minimal_agent_payload(), headers=_bearer(token)
        ).json()
        agent_id = created["agent_id"]
        client.post(f"/api/v1/agents/{agent_id}/runs/seed-demo", headers=_bearer(token))

        response = client.get(
            f"/api/v1/agents/{agent_id}/runs",
            params={"status_filter": "FAILED"},
            headers=_bearer(token),
        )
        assert response.status_code == 200
        items = response.json()["items"]
        assert len(items) == 1
        assert items[0]["status"] == "FAILED"


async def test_get_run_detail(make_user_and_token, monkeypatch) -> None:
    monkeypatch.setattr(settings, "seed_runs_enabled", True)
    _, token = await make_user_and_token(TENANT_A, role="developer")

    with TestClient(app) as client:
        created = client.post(
            "/api/v1/agents", json=_minimal_agent_payload(), headers=_bearer(token)
        ).json()
        agent_id = created["agent_id"]
        seeded = client.post(
            f"/api/v1/agents/{agent_id}/runs/seed-demo", headers=_bearer(token)
        ).json()["items"]
        run_id = seeded[0]["run_id"]

        response = client.get(
            f"/api/v1/agents/{agent_id}/runs/{run_id}", headers=_bearer(token)
        )
        assert response.status_code == 200
        assert response.json()["run_id"] == run_id


async def test_get_run_404_for_unknown_run(make_user_and_token) -> None:
    _, token = await make_user_and_token(TENANT_A, role="developer")

    with TestClient(app) as client:
        created = client.post(
            "/api/v1/agents", json=_minimal_agent_payload(), headers=_bearer(token)
        ).json()
        agent_id = created["agent_id"]

        response = client.get(
            f"/api/v1/agents/{agent_id}/runs/RUN-DOESNOTEXIST", headers=_bearer(token)
        )
        assert response.status_code == 404


async def test_runs_cross_tenant_returns_404(make_user_and_token, monkeypatch) -> None:
    monkeypatch.setattr(settings, "seed_runs_enabled", True)
    _, token_a = await make_user_and_token(TENANT_A, role="developer")
    _, token_b = await make_user_and_token(TENANT_B, role="developer")

    with TestClient(app) as client:
        created = client.post(
            "/api/v1/agents", json=_minimal_agent_payload(), headers=_bearer(token_a)
        ).json()
        agent_id = created["agent_id"]
        client.post(f"/api/v1/agents/{agent_id}/runs/seed-demo", headers=_bearer(token_a))

        response = client.get(
            f"/api/v1/agents/{agent_id}/runs", headers=_bearer(token_b)
        )
    assert response.status_code == 404


async def test_seed_demo_forbidden_for_auditor(make_user_and_token, monkeypatch) -> None:
    monkeypatch.setattr(settings, "seed_runs_enabled", True)
    _, dev_token = await make_user_and_token(TENANT_A, role="developer")
    _, auditor_token = await make_user_and_token(TENANT_A, role="auditor")

    with TestClient(app) as client:
        created = client.post(
            "/api/v1/agents", json=_minimal_agent_payload(), headers=_bearer(dev_token)
        ).json()
        agent_id = created["agent_id"]

        response = client.post(
            f"/api/v1/agents/{agent_id}/runs/seed-demo", headers=_bearer(auditor_token)
        )
    assert response.status_code == 403


async def test_auditor_can_list_runs(make_user_and_token, monkeypatch) -> None:
    monkeypatch.setattr(settings, "seed_runs_enabled", True)
    _, dev_token = await make_user_and_token(TENANT_A, role="developer")
    _, auditor_token = await make_user_and_token(TENANT_A, role="auditor")

    with TestClient(app) as client:
        created = client.post(
            "/api/v1/agents", json=_minimal_agent_payload(), headers=_bearer(dev_token)
        ).json()
        agent_id = created["agent_id"]
        client.post(f"/api/v1/agents/{agent_id}/runs/seed-demo", headers=_bearer(dev_token))

        response = client.get(
            f"/api/v1/agents/{agent_id}/runs", headers=_bearer(auditor_token)
        )
    assert response.status_code == 200
    assert len(response.json()["items"]) == 4


async def test_seed_demo_populates_rag_and_ragas(make_user_and_token, monkeypatch) -> None:
    """Phase 3, Section 9 — the SUCCESS run's Knowledge Base step carries a
    RAG retrieval detail (query redacted per R30) and the run itself
    carries RAGAS scores."""
    monkeypatch.setattr(settings, "seed_runs_enabled", True)
    _, token = await make_user_and_token(TENANT_A, role="developer")

    with TestClient(app) as client:
        created = client.post(
            "/api/v1/agents", json=_minimal_agent_payload(), headers=_bearer(token)
        ).json()
        agent_id = created["agent_id"]
        seeded = client.post(
            f"/api/v1/agents/{agent_id}/runs/seed-demo", headers=_bearer(token)
        ).json()["items"]

    success = next(r for r in seeded if r["status"] == "SUCCESS" and r["trigger"] == "API")
    kb_step = next(s for s in success["steps"] if s["component"] == "Knowledge Base")
    assert kb_step["rag"]["query"] == "[REDACTED]"
    assert kb_step["rag"]["documents_returned"] == 5
    assert kb_step["rag"]["relevant_count"] == 4
    assert len(kb_step["rag"]["documents"]) == 5

    assert success["ragas_scores"]["faithfulness"] == 0.94
    assert set(success["ragas_scores"].keys()) == {
        "faithfulness",
        "answer_relevance",
        "context_precision",
        "context_recall",
        "context_relevance",
    }


async def test_seed_demo_populates_span_tree(make_user_and_token, monkeypatch) -> None:
    """Phase 3, Section 7 — the SUCCESS run has a real parent/child span
    tree, not just a flat list, and every span's attributes are plain
    strings (R30/R45 — never raw prompt/response content)."""
    monkeypatch.setattr(settings, "seed_runs_enabled", True)
    _, token = await make_user_and_token(TENANT_A, role="developer")

    with TestClient(app) as client:
        created = client.post(
            "/api/v1/agents", json=_minimal_agent_payload(), headers=_bearer(token)
        ).json()
        agent_id = created["agent_id"]
        seeded = client.post(
            f"/api/v1/agents/{agent_id}/runs/seed-demo", headers=_bearer(token)
        ).json()["items"]

    success = next(r for r in seeded if r["status"] == "SUCCESS" and r["trigger"] == "API")
    spans = success["spans"]
    assert len(spans) >= 4
    root = next(s for s in spans if s["parent_span_id"] is None)
    children = [s for s in spans if s["parent_span_id"] == root["span_id"]]
    assert len(children) >= 1
    for span in spans:
        for value in span["attributes"].values():
            assert isinstance(value, str)


async def test_runs_summary_aggregates_seeded_runs(make_user_and_token, monkeypatch) -> None:
    """Phase 3, Section 10 — agent-level 7-day summary derived from the
    seeded runs (2 SUCCESS, 1 FAILED, 1 RUNNING with no duration yet)."""
    monkeypatch.setattr(settings, "seed_runs_enabled", True)
    _, token = await make_user_and_token(TENANT_A, role="developer")

    with TestClient(app) as client:
        created = client.post(
            "/api/v1/agents", json=_minimal_agent_payload(), headers=_bearer(token)
        ).json()
        agent_id = created["agent_id"]
        client.post(f"/api/v1/agents/{agent_id}/runs/seed-demo", headers=_bearer(token))

        response = client.get(f"/api/v1/agents/{agent_id}/runs/summary", headers=_bearer(token))

    assert response.status_code == 200
    body = response.json()
    assert body["total_runs"] == 4
    assert body["error_count"] == 1
    assert body["success_rate"] == 0.5
    assert body["total_tokens"] > 0
    assert body["estimated_cost_usd"] > 0


async def test_runs_summary_empty_when_no_runs(make_user_and_token) -> None:
    _, token = await make_user_and_token(TENANT_A, role="developer")

    with TestClient(app) as client:
        created = client.post(
            "/api/v1/agents", json=_minimal_agent_payload(), headers=_bearer(token)
        ).json()
        agent_id = created["agent_id"]

        response = client.get(f"/api/v1/agents/{agent_id}/runs/summary", headers=_bearer(token))

    assert response.status_code == 200
    body = response.json()
    assert body["total_runs"] == 0
    assert body["success_rate"] is None
    assert body["avg_latency_ms"] is None


async def test_get_run_logs_sourced_from_activity(make_user_and_token, monkeypatch) -> None:
    """Phase 3, Section 8 — Logs tab. No real CloudWatch proxy exists yet
    (see LogLine's docstring), so this reuses the already-sanitised
    Activity Feed as its source."""
    monkeypatch.setattr(settings, "seed_runs_enabled", True)
    _, token = await make_user_and_token(TENANT_A, role="developer")

    with TestClient(app) as client:
        created = client.post(
            "/api/v1/agents", json=_minimal_agent_payload(), headers=_bearer(token)
        ).json()
        agent_id = created["agent_id"]
        seeded = client.post(
            f"/api/v1/agents/{agent_id}/runs/seed-demo", headers=_bearer(token)
        ).json()["items"]
        run_id = seeded[0]["run_id"]

        response = client.get(
            f"/api/v1/agents/{agent_id}/runs/{run_id}/logs", headers=_bearer(token)
        )

    assert response.status_code == 200
    lines = response.json()["lines"]
    assert len(lines) == len(seeded[0]["activity"])
    for line in lines:
        assert isinstance(line["message"], str)


async def test_get_run_logs_404_for_unknown_run(make_user_and_token) -> None:
    _, token = await make_user_and_token(TENANT_A, role="developer")

    with TestClient(app) as client:
        created = client.post(
            "/api/v1/agents", json=_minimal_agent_payload(), headers=_bearer(token)
        ).json()
        agent_id = created["agent_id"]

        response = client.get(
            f"/api/v1/agents/{agent_id}/runs/RUN-DOESNOTEXIST/logs", headers=_bearer(token)
        )
    assert response.status_code == 404
