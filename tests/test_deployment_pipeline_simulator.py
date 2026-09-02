"""DeploymentPipelineSimulator (SIMULATE_DEPLOYMENT_PIPELINE) — end-to-end
through the real app, same pattern as test_deploy_api.py /
test_deployment_approval_api.py, but with app.state.deployment_pipeline_simulator
swapped for a zero-delay instance so the test doesn't wait on real sleeps.

Confirms the simulator (not a real customer CI/CD, which doesn't exist in
local dev — see the module's own docstring) drives a triggered deployment
all the way to ACTIVE, merging the PR for a v2+ deploy and parking a
"manual"-mode deployment at PENDING_APPROVAL until approved.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.config import settings
from app.main import app
from app.modules.deployment.iac_scan_runner import IaCScanResult
from app.modules.deployment.pipeline_simulator import DeploymentPipelineSimulator
from app.modules.iac_generator.validation_models import CheckResult, IaCValidationReport
from app.modules.security.models import SecurityScanSummary
from tests.fakes import FakeGitProvider

TENANT_A = "tenant-a"


class _StubScanRunner:
    """Stands in for a real IaCScanRunner — returns a fixed, pre-baked
    policy decision instead of actually invoking tfsec/checkov/terraform,
    so these tests stay fast and independent of what's installed on the
    machine running them (see test_iac_scan_runner.py for the real
    tfsec/checkov output parsing coverage)."""

    def __init__(self, decision: str) -> None:
        self.decision = decision
        self.calls: list[dict[str, Any]] = []

    async def run(self, **kwargs: Any) -> IaCScanResult:
        self.calls.append(kwargs)
        return IaCScanResult(
            security_summary=SecurityScanSummary(
                scan_type="iac_scan",
                passed=True,
                findings=[],
                summary="tfsec + checkov: 0 findings",
            ),
            validation_report=IaCValidationReport(
                passed=True,
                tool="terraform",
                generated_at="2026-01-01T00:00:00Z",
                checks=[CheckResult(name="stub_check", passed=True, detail="ok")],
            ),
            policy_decision=self.decision,
            policy_reason=(
                "All security gates passed" if self.decision == "PASS" else "Critical finding: test"
            ),
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


@pytest.fixture(autouse=True)
def _simulate_pipeline(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "simulate_deployment_pipeline", True)


def _install_fast_simulator(fake_git: FakeGitProvider, scan_runner: Any = None) -> None:
    app.state.git_provider = fake_git
    app.state.deployment_pipeline_simulator = DeploymentPipelineSimulator(
        app.state.deployment_status_store,
        app.state.registry_store,
        fake_git,
        settings,
        stage_delay_seconds=0,
        iac_scan_runner=scan_runner,
    )


async def test_simulated_pipeline_drives_v1_deploy_to_active(make_user_and_token) -> None:
    _, token = await make_user_and_token(TENANT_A, role="developer")

    with TestClient(app) as client:
        _install_fast_simulator(FakeGitProvider())

        created = client.post(
            "/api/v1/agents", json=_minimal_agent_payload(), headers=_bearer(token)
        ).json()
        agent_id = created["agent_id"]

        deployed = client.post(f"/api/v1/agents/{agent_id}/deploy", headers=_bearer(token)).json()

        deployment_detail = client.get(
            f"/api/v1/deployments/{deployed['deployment_id']}", headers=_bearer(token)
        ).json()
        agent_detail = client.get(f"/api/v1/agents/{agent_id}", headers=_bearer(token)).json()

    assert deployment_detail["status"] == "ACTIVE"
    assert all(stage["status"] == "PASSED" for stage in deployment_detail["stages"].values())
    assert agent_detail["agent"]["status"] == "ACTIVE"
    assert agent_detail["agent"]["live_version"] == 1


async def test_simulated_pipeline_merges_pr_for_v2_plus_deploy(make_user_and_token) -> None:
    _, token = await make_user_and_token(TENANT_A, role="developer")

    with TestClient(app) as client:
        created = client.post(
            "/api/v1/agents", json=_minimal_agent_payload(), headers=_bearer(token)
        ).json()
        agent_id = created["agent_id"]
        repo = f"test-org/panasa-iac-{agent_id}"

        fake_git = FakeGitProvider(existing_repos={repo})
        _install_fast_simulator(fake_git)

        deployed = client.post(f"/api/v1/agents/{agent_id}/deploy", headers=_bearer(token)).json()

        deployment_detail = client.get(
            f"/api/v1/deployments/{deployed['deployment_id']}", headers=_bearer(token)
        ).json()

    assert deployment_detail["status"] == "ACTIVE"
    assert fake_git.merged == [(repo, deployed["pull_request_id"])]


async def test_simulated_pipeline_parks_manual_mode_at_pending_approval(
    make_user_and_token,
) -> None:
    admin_user, admin_token = await make_user_and_token(TENANT_A, role="admin")
    dev_user, dev_token = await make_user_and_token(TENANT_A, role="developer", email=None)

    with TestClient(app) as client:
        client.patch(
            "/api/v1/admin/settings/deployment",
            json={"default_approval_mode": "manual"},
            headers=_bearer(admin_token),
        )

        _install_fast_simulator(FakeGitProvider())

        created = client.post(
            "/api/v1/agents", json=_minimal_agent_payload(), headers=_bearer(dev_token)
        ).json()
        agent_id = created["agent_id"]

        deployed = client.post(
            f"/api/v1/agents/{agent_id}/deploy", headers=_bearer(dev_token)
        ).json()

        parked = client.get(
            f"/api/v1/deployments/{deployed['deployment_id']}", headers=_bearer(dev_token)
        ).json()

    assert parked["status"] == "PENDING_APPROVAL"
    assert parked["stages"]["POLICY_CHECK"]["status"] == "PASSED"
    assert parked["stages"]["APPLYING"]["status"] == "PENDING"

    _ = admin_user, dev_user  # only used to obtain tokens


async def test_simulated_pipeline_resumes_to_active_after_approval(
    make_user_and_token,
) -> None:
    admin_user, admin_token = await make_user_and_token(TENANT_A, role="admin")
    dev_user, dev_token = await make_user_and_token(TENANT_A, role="developer", email=None)

    with TestClient(app) as client:
        client.patch(
            "/api/v1/admin/settings/deployment",
            json={"default_approval_mode": "manual"},
            headers=_bearer(admin_token),
        )

        fake_git = FakeGitProvider()
        _install_fast_simulator(fake_git)

        created = client.post(
            "/api/v1/agents", json=_minimal_agent_payload(), headers=_bearer(dev_token)
        ).json()
        agent_id = created["agent_id"]

        deployed = client.post(
            f"/api/v1/agents/{agent_id}/deploy", headers=_bearer(dev_token)
        ).json()
        deployment_id = deployed["deployment_id"]

        client.post(
            f"/api/v1/agents/{agent_id}/deployments/{deployment_id}/approve",
            headers=_bearer(dev_token),
        )

        final_detail = client.get(
            f"/api/v1/deployments/{deployment_id}", headers=_bearer(dev_token)
        ).json()
        agent_detail = client.get(f"/api/v1/agents/{agent_id}", headers=_bearer(dev_token)).json()

    assert final_detail["status"] == "ACTIVE"
    assert agent_detail["agent"]["status"] == "ACTIVE"
    assert agent_detail["agent"]["live_version"] == 1

    _ = admin_user  # only used to obtain admin_token


async def test_real_scan_runner_populates_real_stage_output(make_user_and_token) -> None:
    """When an IaCScanRunner is supplied, SECURITY_SCANNING/
    TERRAFORM_VALIDATE/POLICY_CHECK carry genuine scan/validation output —
    never the "[simulated] ... passed." text every other stage gets."""
    _, token = await make_user_and_token(TENANT_A, role="developer")

    with TestClient(app) as client:
        scan_runner = _StubScanRunner(decision="PASS")
        _install_fast_simulator(FakeGitProvider(), scan_runner=scan_runner)

        created = client.post(
            "/api/v1/agents", json=_minimal_agent_payload(), headers=_bearer(token)
        ).json()
        agent_id = created["agent_id"]

        deployed = client.post(f"/api/v1/agents/{agent_id}/deploy", headers=_bearer(token)).json()

        deployment_detail = client.get(
            f"/api/v1/deployments/{deployed['deployment_id']}", headers=_bearer(token)
        ).json()

    assert deployment_detail["status"] == "ACTIVE"
    stages = deployment_detail["stages"]
    assert stages["SECURITY_SCANNING"]["output_summary"] == "tfsec + checkov: 0 findings"
    assert "[simulated]" not in stages["SECURITY_SCANNING"]["output_summary"]
    assert "stub_check" in stages["TERRAFORM_VALIDATE"]["output_summary"]
    assert stages["POLICY_CHECK"]["output_summary"] == "All security gates passed"
    assert len(scan_runner.calls) == 1
    assert scan_runner.calls[0]["agent_id"] == agent_id


async def test_real_scan_runner_block_decision_stops_pipeline(make_user_and_token) -> None:
    """A real POLICY_CHECK BLOCK must close the PR, mark the agent BLOCKED
    (no prior live_version to fall back to — R22), and never reach
    APPLYING/ACTIVE."""
    _, token = await make_user_and_token(TENANT_A, role="developer")

    with TestClient(app) as client:
        created = client.post(
            "/api/v1/agents", json=_minimal_agent_payload(), headers=_bearer(token)
        ).json()
        agent_id = created["agent_id"]
        repo = f"test-org/panasa-iac-{agent_id}"

        fake_git = FakeGitProvider(existing_repos={repo})  # v2+ deploy — opens a real PR to close
        scan_runner = _StubScanRunner(decision="BLOCK")
        _install_fast_simulator(fake_git, scan_runner=scan_runner)

        deployed = client.post(f"/api/v1/agents/{agent_id}/deploy", headers=_bearer(token)).json()

        deployment_detail = client.get(
            f"/api/v1/deployments/{deployed['deployment_id']}", headers=_bearer(token)
        ).json()
        agent_detail = client.get(f"/api/v1/agents/{agent_id}", headers=_bearer(token)).json()

    assert deployment_detail["status"] == "BLOCKED"
    assert deployment_detail["stages"]["POLICY_CHECK"]["status"] == "BLOCKED"
    assert deployment_detail["stages"]["APPLYING"]["status"] == "PENDING"
    assert agent_detail["agent"]["status"] == "BLOCKED"
    assert agent_detail["agent"]["live_version"] is None
    assert fake_git.merged == []
    assert fake_git.closed == [(repo, deployed["pull_request_id"], "Critical finding: test")]
