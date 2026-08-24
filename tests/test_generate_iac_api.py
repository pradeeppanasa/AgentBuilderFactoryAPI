from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.config import settings
from app.main import app
from app.modules.iac_generator.validation_models import CheckResult, IaCValidationReport

TENANT_A = "tenant-a"
TENANT_B = "tenant-b"


class _AlwaysFailsValidator:
    """R40: POST /generate-iac must return 422 with the full report — never
    a 200 wrapping an unvalidated/failed bundle — when validation fails."""

    async def validate(self, **kwargs: Any) -> IaCValidationReport:
        return IaCValidationReport(
            passed=False,
            tool=kwargs.get("tool", "terraform"),
            generated_at="2026-01-01T00:00:00+00:00",
            checks=[
                CheckResult(name="naming_convention", passed=False, detail="forced test failure")
            ],
        )


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _minimal_agent_payload(name: str = "KYC Agent") -> dict[str, Any]:
    return {
        "name": name,
        "description": "Know Your Customer verification agent",
        "business_purpose": "Automate KYC document verification for onboarding",
        "agent_type": "standard",
        "configuration": {
            "model_id": "anthropic.claude-3-5-sonnet-20241022-v2:0",
            "model_provider": "bedrock",
            "system_prompt": "You are a KYC verification agent for {{company_name}}.",
        },
    }


async def test_generate_iac_happy_path_persists_artifact(make_user_and_token) -> None:
    _, token = await make_user_and_token(TENANT_A, role="developer")

    with TestClient(app) as client:
        created = client.post(
            "/api/v1/agents", json=_minimal_agent_payload(), headers=_bearer(token)
        ).json()
        agent_id = created["agent_id"]

        response = client.post(f"/api/v1/agents/{agent_id}/generate-iac", headers=_bearer(token))

        assert response.status_code == 200
        body = response.json()
        assert body["agent_id"] == agent_id
        assert body["version"] == 1
        assert body["tool"] == "terraform"
        assert body["s3_key"]
        assert "base" in body["modules"]
        assert body["validation_report"]["passed"] is True
        assert body["validation_report"]["tool"] == "terraform"
        check_names = {c["name"] for c in body["validation_report"]["checks"]}
        assert "naming_convention" in check_names
        assert "tagging" in check_names
        assert "resource_presence" in check_names

        # Development Terraform Validation Mode: "local" is the default and
        # never carries a real-deployment note.
        assert body["validation_mode"] == "local"
        assert body["environment_note"] is None

        detail = client.get(f"/api/v1/agents/{agent_id}/versions/1", headers=_bearer(token)).json()

    assert detail["iac_s3_key"] == body["s3_key"]
    assert detail["iac_version"] == body["iac_version"]
    # Stored alongside the generated IaC (CLAUDE.md Section 6) and visible
    # via the existing version-detail read path — no separate GET route.
    assert detail["iac_validation_report"]["passed"] is True
    assert detail["iac_validation_report"] == body["validation_report"]


async def test_generate_iac_reflects_new_config_after_edit_and_regenerate(
    make_user_and_token,
) -> None:
    """I-03 (CLAUDE.md Section 39.8: "IaC generator uses stale agent data on
    regenerate") — regression coverage. generate-iac always re-fetches
    agent.current_version fresh from the store (app/api/v1/agents.py), so
    editing an agent (which creates a new version, R08) and regenerating
    must produce IaC for the NEW config, never the version generate-iac
    last ran against."""
    _, token = await make_user_and_token(TENANT_A, role="developer")

    with TestClient(app) as client:
        created = client.post(
            "/api/v1/agents", json=_minimal_agent_payload(), headers=_bearer(token)
        ).json()
        agent_id = created["agent_id"]

        v1_response = client.post(
            f"/api/v1/agents/{agent_id}/generate-iac", headers=_bearer(token)
        ).json()
        assert v1_response["version"] == 1
        assert "tools" not in v1_response["modules"]

        v2_config = _minimal_agent_payload()["configuration"]
        v2_config["tools"] = [
            {
                "tool_id": "jira",
                "tool_name": "Jira",
                "executor_type": "http",
                "endpoint": "https://acme.atlassian.net/rest/api/3",
                "input_schema": {},
            }
        ]
        put_response = client.put(
            f"/api/v1/agents/{agent_id}",
            json={"configuration": v2_config, "change_description": "Add Jira tool"},
            headers=_bearer(token),
        )
        assert put_response.status_code == 200
        assert put_response.json()["version"] == 2

        v2_response = client.post(
            f"/api/v1/agents/{agent_id}/generate-iac", headers=_bearer(token)
        ).json()
        assert v2_response["version"] == 2
        assert "tools" in v2_response["modules"]
        assert v2_response["s3_key"] != v1_response["s3_key"]

        v1_detail = client.get(
            f"/api/v1/agents/{agent_id}/versions/1", headers=_bearer(token)
        ).json()
        v2_detail = client.get(
            f"/api/v1/agents/{agent_id}/versions/2", headers=_bearer(token)
        ).json()

    # Each version's own recorded artifact stays exactly what generate-iac
    # produced for it — v1's record is never overwritten by v2's run.
    assert v1_detail["iac_s3_key"] == v1_response["s3_key"]
    assert "tools" not in v1_detail["iac_modules"]
    assert v2_detail["iac_s3_key"] == v2_response["s3_key"]
    assert "tools" in v2_detail["iac_modules"]


async def test_generate_iac_422_when_validation_fails(make_user_and_token) -> None:
    _, token = await make_user_and_token(TENANT_A, role="developer")

    with TestClient(app) as client:
        app.state.iac_validator = _AlwaysFailsValidator()

        created = client.post(
            "/api/v1/agents", json=_minimal_agent_payload(), headers=_bearer(token)
        ).json()
        agent_id = created["agent_id"]

        response = client.post(f"/api/v1/agents/{agent_id}/generate-iac", headers=_bearer(token))

        assert response.status_code == 422
        body = response.json()
        assert body["detail"]["passed"] is False
        assert body["detail"]["checks"][0]["name"] == "naming_convention"
        assert body["detail"]["checks"][0]["passed"] is False

        # R40: still persisted for later inspection ("why did this fail
        # last Tuesday") — just never handed back as a usable 200 response.
        detail = client.get(f"/api/v1/agents/{agent_id}/versions/1", headers=_bearer(token)).json()

    assert detail["iac_validation_report"]["passed"] is False


async def test_generate_iac_404_for_unknown_agent(make_user_and_token) -> None:
    _, token = await make_user_and_token(TENANT_A, role="developer")

    with TestClient(app) as client:
        response = client.post("/api/v1/agents/does-not-exist/generate-iac", headers=_bearer(token))

    assert response.status_code == 404


async def test_generate_iac_forbidden_for_auditor(make_user_and_token) -> None:
    _, developer_token = await make_user_and_token(TENANT_A, role="developer")
    _, auditor_token = await make_user_and_token(TENANT_A, role="auditor")

    with TestClient(app) as client:
        created = client.post(
            "/api/v1/agents", json=_minimal_agent_payload(), headers=_bearer(developer_token)
        ).json()
        agent_id = created["agent_id"]

        response = client.post(
            f"/api/v1/agents/{agent_id}/generate-iac", headers=_bearer(auditor_token)
        )

    assert response.status_code == 403


async def test_non_local_validation_mode_forbidden_by_default(make_user_and_token) -> None:
    """dev_validation_extended_modes_enabled defaults to False — the
    Panasa VPC / Customer VPC placeholder modes must stay hidden unless an
    operator deliberately opts in."""
    _, token = await make_user_and_token(TENANT_A, role="developer")

    with TestClient(app) as client:
        created = client.post(
            "/api/v1/agents", json=_minimal_agent_payload(), headers=_bearer(token)
        ).json()
        agent_id = created["agent_id"]

        response = client.post(
            f"/api/v1/agents/{agent_id}/generate-iac",
            params={"validation_mode": "panasa_vpc"},
            headers=_bearer(token),
        )
        assert response.status_code == 403

        # Nothing was generated or persisted for the disallowed call.
        detail = client.get(f"/api/v1/agents/{agent_id}/versions/1", headers=_bearer(token)).json()
    assert detail["iac_s3_key"] is None


async def test_non_local_validation_mode_allowed_when_enabled(
    make_user_and_token, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Never a real deployment even when enabled — same local generate +
    validate as the default mode, just labelled with an explanatory note
    and no AWS account contacted (IaCValidator is unchanged either way)."""
    monkeypatch.setattr(settings, "dev_validation_extended_modes_enabled", True)
    _, token = await make_user_and_token(TENANT_A, role="developer")

    with TestClient(app) as client:
        created = client.post(
            "/api/v1/agents", json=_minimal_agent_payload(), headers=_bearer(token)
        ).json()
        agent_id = created["agent_id"]

        response = client.post(
            f"/api/v1/agents/{agent_id}/generate-iac",
            params={"validation_mode": "customer_vpc"},
            headers=_bearer(token),
        )
        assert response.status_code == 200
        body = response.json()
        assert body["validation_mode"] == "customer_vpc"
        assert body["environment_note"] is not None
        assert "not implemented in Stage 1" in body["environment_note"]
        assert "no aws account was contacted" in body["environment_note"].lower()
        # Still ran the real, AWS-independent local checks.
        assert body["validation_report"]["passed"] is True


async def test_non_local_validation_mode_forbidden_for_auditor_even_when_enabled(
    make_user_and_token, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "dev_validation_extended_modes_enabled", True)
    _, developer_token = await make_user_and_token(TENANT_A, role="developer")
    _, auditor_token = await make_user_and_token(TENANT_A, role="auditor")

    with TestClient(app) as client:
        created = client.post(
            "/api/v1/agents", json=_minimal_agent_payload(), headers=_bearer(developer_token)
        ).json()
        agent_id = created["agent_id"]

        response = client.post(
            f"/api/v1/agents/{agent_id}/generate-iac",
            params={"validation_mode": "panasa_vpc"},
            headers=_bearer(auditor_token),
        )
        assert response.status_code == 403


async def test_invalid_validation_mode_returns_422(make_user_and_token) -> None:
    _, token = await make_user_and_token(TENANT_A, role="developer")

    with TestClient(app) as client:
        created = client.post(
            "/api/v1/agents", json=_minimal_agent_payload(), headers=_bearer(token)
        ).json()
        agent_id = created["agent_id"]

        response = client.post(
            f"/api/v1/agents/{agent_id}/generate-iac",
            params={"validation_mode": "bogus_mode"},
            headers=_bearer(token),
        )
        assert response.status_code == 422


async def test_generate_iac_cross_tenant_returns_404(make_user_and_token) -> None:
    _, token_a = await make_user_and_token(TENANT_A, role="developer")
    _, token_b = await make_user_and_token(TENANT_B, role="developer")

    with TestClient(app) as client:
        created = client.post(
            "/api/v1/agents", json=_minimal_agent_payload(), headers=_bearer(token_a)
        ).json()
        agent_id = created["agent_id"]

        response = client.post(f"/api/v1/agents/{agent_id}/generate-iac", headers=_bearer(token_b))

    assert response.status_code == 404


async def test_iac_status_not_started_before_first_generate(make_user_and_token) -> None:
    """Wizard Redesign QA A-04/U-08 — before generate-iac has ever run for
    this version, the status endpoint reports not_started with no stages,
    rather than 404ing or fabricating progress."""
    _, token = await make_user_and_token(TENANT_A, role="developer")

    with TestClient(app) as client:
        created = client.post(
            "/api/v1/agents", json=_minimal_agent_payload(), headers=_bearer(token)
        ).json()
        agent_id = created["agent_id"]

        response = client.get(f"/api/v1/agents/{agent_id}/iac/status", headers=_bearer(token))

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "not_started"
    assert body["stages"] == []
    assert body["validation"] is None


async def test_iac_status_completed_after_generate(make_user_and_token) -> None:
    """generate-iac renders + validates synchronously (see IaCStatusResponse's
    docstring) — a poll right after triggering it must already report
    completed with every resolved module marked as a completed stage."""
    _, token = await make_user_and_token(TENANT_A, role="developer")

    with TestClient(app) as client:
        created = client.post(
            "/api/v1/agents", json=_minimal_agent_payload(), headers=_bearer(token)
        ).json()
        agent_id = created["agent_id"]

        generate_response = client.post(
            f"/api/v1/agents/{agent_id}/generate-iac", headers=_bearer(token)
        )
        assert generate_response.status_code == 200
        modules = generate_response.json()["modules"]

        response = client.get(f"/api/v1/agents/{agent_id}/iac/status", headers=_bearer(token))

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "completed"
    assert body["version"] == 1
    assert {s["name"] for s in body["stages"]} == set(modules)
    assert all(s["status"] == "completed" for s in body["stages"])
    assert body["validation"]["passed"] is True


async def test_iac_status_reports_failed_when_validation_failed(make_user_and_token) -> None:
    _, token = await make_user_and_token(TENANT_A, role="developer")

    with TestClient(app) as client:
        app.state.iac_validator = _AlwaysFailsValidator()

        created = client.post(
            "/api/v1/agents", json=_minimal_agent_payload(), headers=_bearer(token)
        ).json()
        agent_id = created["agent_id"]

        client.post(f"/api/v1/agents/{agent_id}/generate-iac", headers=_bearer(token))

        response = client.get(f"/api/v1/agents/{agent_id}/iac/status", headers=_bearer(token))

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "failed"
    assert body["validation"]["passed"] is False


async def test_iac_status_forbidden_for_wrong_tenant(make_user_and_token) -> None:
    _, token_a = await make_user_and_token(TENANT_A, role="developer")
    _, token_b = await make_user_and_token(TENANT_B, role="developer")

    with TestClient(app) as client:
        created = client.post(
            "/api/v1/agents", json=_minimal_agent_payload(), headers=_bearer(token_a)
        ).json()
        agent_id = created["agent_id"]

        response = client.get(f"/api/v1/agents/{agent_id}/iac/status", headers=_bearer(token_b))

    assert response.status_code == 404
