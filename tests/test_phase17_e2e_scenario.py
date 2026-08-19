"""Phase 17 — End-to-end test (CLAUDE.md Section 14's original Phase
Implementation Order: "Full scenario (automated pytest + manual
verification)"). Drives the exact 8-step scenario the spec names.

What this test genuinely exercises, end to end, through real code:
  - The real FastAPI app over real HTTP (TestClient), not a shortcut around it.
  - Real DynamoDB/S3/Secrets Manager/EventBridge via moto (conftest.py's
    autouse mocked_aws fixture) — not hand-rolled fakes for these.
  - The REAL lambda_handlers/*.py modules that Step Functions would invoke
    (validating, change_impact, generating_iac, policy_check, deploying,
    health_check, mark_active, mark_blocked) — called directly, in the
    exact order and with the exact event-shape merging
    step_functions/deployment_workflow.json's ResultPath chaining performs,
    since there is no real Step Functions/CodeBuild to execute them for us
    (see the module docstring below on why that's a hard environment limit,
    not a shortcut we chose).
  - The real ChangeImpactAnalyzer, policy_gate(), and AgentRegistryStore
    version/rollback logic.

What is SIMULATED, and why — this is the one deliberate seam:
  SECURITY_SCANNING / EVALUATING / TERRAFORM_VALIDATE / TERRAFORM_PLAN /
  APPLYING are, per the actual deployment_workflow.json, CodeBuild jobs
  (`arn:aws:states:::codebuild:startBuild.sync`), not Lambda functions —
  and per F0/F2/R05, running terraform and security scanners against real
  infrastructure is explicitly the CUSTOMER'S OWN CI/CD's job, never this
  Runtime's. There is no "local" version of a customer's CI/CD to execute.
  This test stands in for those CodeBuild jobs by writing the exact stage
  result DynamoDB records they would write on completion
  (codebuild/scripts/write_stage_result.sh's contract) — the same
  DeploymentStatusStore.update_stage() call, with the same stage-name and
  blocking_issue conventions policy_check.py already depends on and is
  tested against elsewhere (tests/test_policy_enforcement.py). No
  Panasa-owned decision logic is bypassed: policy_check.handler still reads
  these written results and makes its own real PASS/BLOCK call.

Step 4 of the spec ("Manually delete a Lambda → edit agent → terraform plan
detects missing → recreates → ACTIVE") is NOT executed here — see
test_step4_terraform_drift_recreation_not_executable_in_this_environment's
docstring for exactly why, and what it demonstrates instead.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.main import app
from tests.fakes import FakeGitProvider

TENANT = "tenant-kyc-e2e"

# lambda_handlers.common fetches the git token and builds AWS clients at
# MODULE IMPORT time (by design — it's meant to run inside a warm Lambda
# execution environment with real credentials already present, see that
# module's docstring). Importing it at test-collection time, before
# conftest.py's autouse mocked_aws fixture has entered its mock_aws()
# context, fails with NoCredentialsError. All lambda_handlers imports are
# therefore deferred to first use inside a test function body.


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _kyc_configuration(
    system_prompt: str = "You are a KYC verification agent for {{company_name}}.",
    tools: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return {
        "model_id": "anthropic.claude-3-5-sonnet-20241022-v2:0",
        "model_provider": "bedrock",
        "system_prompt": system_prompt,
        "guardrails": {
            "prompt_injection": True,
            "pii_detection": True,
            "toxicity_filter": True,
            "hallucination_check": True,
            "pii_strip_output": True,
        },
        "knowledge_base": {
            "enabled": True,
            "kb_name": "kyc-documents",
            "s3_bucket": "customer-kyc-kb",
            "top_k": 5,
        },
        "tools": (
            tools
            if tools is not None
            else [
                {
                    "tool_id": "jira",
                    "tool_name": "Jira",
                    "executor_type": "http",
                    "endpoint": "https://acme.atlassian.net/rest/api/3",
                }
            ]
        ),
        "human_review": {
            "enabled": True,
            "trigger_conditions": ["high_risk_decision"],
            "approval_timeout_hours": 4,
        },
    }


def _run_simulated_pipeline(
    *, agent_id: str, tenant_id: str, version: int, deployment_id: str, inject_critical: bool
) -> dict[str, Any]:
    """Stands in for the Step Functions execution deployment_workflow.json
    describes, calling the same lambda_handlers the real state machine
    would invoke, in the same order, threading each stage's result forward
    under the same key the ASL's ResultPath would (e.g. "generatingIac"),
    and writing CodeBuild-stage results the way a real CodeBuild job's
    buildspec would (see module docstring)."""
    from lambda_handlers import (
        change_impact,
        deploying,
        generating_iac,
        health_check,
        mark_active,
        mark_blocked,
        policy_check,
        validating,
    )
    from lambda_handlers.common import deployment_status_store, run

    event: dict[str, Any] = {
        "agentId": agent_id,
        "tenantId": tenant_id,
        "version": version,
        "deploymentId": deployment_id,
    }

    validating.handler(event, None)
    event["changeImpact"] = change_impact.handler(event, None)
    event["generatingIac"] = generating_iac.handler(event, None)

    if inject_critical:
        run(
            deployment_status_store.update_stage(
                agent_id=agent_id,
                deployment_id=deployment_id,
                stage="SECURITY_SCANNING",
                stage_status="FAILED",
                output_summary="Container scan (Trivy): 1 CRITICAL CVE in base image",
                blocking_issue="critical_cve_found",
            )
        )
    else:
        run(
            deployment_status_store.update_stage(
                agent_id=agent_id,
                deployment_id=deployment_id,
                stage="SECURITY_SCANNING",
                stage_status="PASSED",
                output_summary="SAST/secret/dependency/IaC/container scans: 0 critical findings",
            )
        )
    run(
        deployment_status_store.update_stage(
            agent_id=agent_id,
            deployment_id=deployment_id,
            stage="EVALUATING",
            stage_status="PASSED",
            output_summary="No RAGAS test dataset configured — evaluation skipped (R14)",
        )
    )
    run(
        deployment_status_store.update_stage(
            agent_id=agent_id,
            deployment_id=deployment_id,
            stage="TERRAFORM_VALIDATE",
            stage_status="PASSED",
            output_summary="terraform validate: configuration is valid",
        )
    )
    run(
        deployment_status_store.update_stage(
            agent_id=agent_id,
            deployment_id=deployment_id,
            stage="TERRAFORM_PLAN",
            stage_status="PASSED",
            output_summary="Plan: 3 to add, 0 to change, 0 to destroy",
        )
    )

    policy_result: dict[str, Any] = policy_check.handler(event, None)
    event["policyCheck"] = policy_result

    if policy_result["result"] == "BLOCK":
        mark_blocked.handler(event, None)
        return policy_result

    run(
        deployment_status_store.update_stage(
            agent_id=agent_id,
            deployment_id=deployment_id,
            stage="APPLYING",
            stage_status="PASSED",
            output_summary="terraform apply: 3 added, 0 changed, 0 destroyed",
        )
    )
    deploying.handler(event, None)
    health_check.handler(event, None)
    mark_active.handler(event, None)
    return policy_result


@pytest.fixture
def fake_git(monkeypatch: pytest.MonkeyPatch) -> FakeGitProvider:
    """One fake shared by the FastAPI layer and the two lambda_handlers
    modules that hold their OWN `git_provider` name binding via
    `from lambda_handlers.common import git_provider` — a plain
    module-attribute patch on lambda_handlers.common would NOT reach those
    (each did its own import-time copy of the reference), so each call site
    is patched individually. Imports are local — see the module-level note
    on why lambda_handlers.common can't be imported at collection time;
    this fixture body only runs once conftest's mocked_aws is active.

    Does NOT set app.state.git_provider here — main.py's lifespan hook
    unconditionally overwrites app.state.git_provider on startup, so that
    swap must happen only AFTER `with TestClient(app) as client:` has
    entered (same ordering test_deploy_api.py's fixture-free version uses).
    """
    from lambda_handlers import generating_iac, policy_check

    git = FakeGitProvider()
    monkeypatch.setattr(generating_iac, "git_provider", git)
    monkeypatch.setattr(policy_check, "git_provider", git)
    return git


async def test_phase17_full_lifecycle_e2e(make_user_and_token, fake_git: FakeGitProvider) -> None:
    from lambda_handlers.common import deployment_status_store

    results: dict[str, str] = {}

    with TestClient(app) as client:
        app.state.git_provider = fake_git
        _, token = await make_user_and_token(TENANT, role="developer")
        headers = _bearer(token)

        # ── Step 1: Create KYC agent (model + KB + Jira tool + guardrails +
        # human review) → deploy → ACTIVE ────────────────────────────────
        create_response = client.post(
            "/api/v1/agents",
            json={
                "name": "KYC Agent",
                "description": "Know Your Customer verification agent",
                "business_purpose": "Automate KYC document verification for onboarding",
                "agent_type": "standard",
                "configuration": _kyc_configuration(),
            },
            headers=headers,
        )
        assert create_response.status_code == 201, create_response.text
        agent_id = create_response.json()["agent_id"]
        assert create_response.json()["version"] == 1
        results["step1_create"] = f"HTTP {create_response.status_code}, agent_id={agent_id}, v1"

        deploy_v1 = client.post(f"/api/v1/agents/{agent_id}/deploy", headers=headers)
        assert deploy_v1.status_code == 200, deploy_v1.text
        deployment_id_v1 = deploy_v1.json()["deployment_id"]
        assert deploy_v1.json()["status"] == "DEPLOYING"
        results["step1_deploy_trigger"] = (
            f"HTTP {deploy_v1.status_code}, status=DEPLOYING, deployment_id={deployment_id_v1}"
        )

        policy_v1 = await asyncio.to_thread(
            _run_simulated_pipeline,
            agent_id=agent_id,
            tenant_id=TENANT,
            version=1,
            deployment_id=deployment_id_v1,
            inject_critical=False,
        )
        assert policy_v1["result"] == "PASS"

        agent_v1 = client.get(f"/api/v1/agents/{agent_id}", headers=headers).json()["agent"]
        assert agent_v1["status"] == "ACTIVE"
        assert agent_v1["live_version"] == 1
        deployment_v1 = await deployment_status_store.get_deployment(agent_id, deployment_id_v1)
        assert deployment_v1 is not None
        assert deployment_v1.status == "ACTIVE"
        results["step1_final"] = (
            f"agent.status=ACTIVE, live_version=1, deployment.status={deployment_v1.status}, "
            f"PR merged={len(fake_git.merged)}"
        )

        # ── Step 2: Edit prompt only → v2 → targeted eval → deploy → ACTIVE
        edited_prompt_config = _kyc_configuration(
            system_prompt="You are a KYC verification agent for {{company_name}}. "
            "Always cite the specific policy clause when rejecting a document."
        )
        update_v2 = client.put(
            f"/api/v1/agents/{agent_id}",
            json={
                "configuration": edited_prompt_config,
                "change_description": "Refine prompt wording",
            },
            headers=headers,
        )
        assert update_v2.status_code == 200, update_v2.text
        assert update_v2.json()["version"] == 2
        results["step2_edit"] = f"HTTP {update_v2.status_code}, v2 created"

        deploy_v2 = client.post(f"/api/v1/agents/{agent_id}/deploy", headers=headers)
        assert deploy_v2.status_code == 200, deploy_v2.text
        deployment_id_v2 = deploy_v2.json()["deployment_id"]

        diff_v2 = client.get(f"/api/v1/agents/{agent_id}/versions/2/diff", headers=headers).json()
        assert diff_v2["impact_analysis"]["impact_level"] == "MEDIUM"
        assert "PROMPT_EVALUATION" in diff_v2["impact_analysis"]["required_validations"]
        results["step2_targeted_eval"] = (
            f"change impact = MEDIUM, required_validations="
            f"{diff_v2['impact_analysis']['required_validations']}"
        )

        policy_v2 = await asyncio.to_thread(
            _run_simulated_pipeline,
            agent_id=agent_id,
            tenant_id=TENANT,
            version=2,
            deployment_id=deployment_id_v2,
            inject_critical=False,
        )
        assert policy_v2["result"] == "PASS"

        agent_v2 = client.get(f"/api/v1/agents/{agent_id}", headers=headers).json()["agent"]
        assert agent_v2["status"] == "ACTIVE"
        assert agent_v2["live_version"] == 2
        results["step2_final"] = "agent.status=ACTIVE, live_version=2"

        # ── Step 3: Add Salesforce tool → v3 → full security + IaC scan →
        # deploy → ACTIVE ──────────────────────────────────────────────
        jira_and_salesforce_config = _kyc_configuration(
            system_prompt=edited_prompt_config["system_prompt"],
            tools=[
                _kyc_configuration()["tools"][0],  # the original Jira tool, unchanged
                {
                    "tool_id": "salesforce",
                    "tool_name": "Salesforce",
                    "executor_type": "http",
                    "endpoint": "https://acme.my.salesforce.com/services/data/v60.0",
                },
            ],
        )
        update_v3 = client.put(
            f"/api/v1/agents/{agent_id}",
            json={
                "configuration": jira_and_salesforce_config,
                "change_description": "Add Salesforce CRM lookup tool",
            },
            headers=headers,
        )
        assert update_v3.status_code == 200, update_v3.text
        assert update_v3.json()["version"] == 3
        results["step3_edit"] = f"HTTP {update_v3.status_code}, v3 created (Jira + Salesforce)"

        deploy_v3 = client.post(f"/api/v1/agents/{agent_id}/deploy", headers=headers)
        assert deploy_v3.status_code == 200, deploy_v3.text
        deployment_id_v3 = deploy_v3.json()["deployment_id"]

        diff_v3 = client.get(f"/api/v1/agents/{agent_id}/versions/3/diff", headers=headers).json()
        assert diff_v3["impact_analysis"]["impact_level"] == "HIGH"
        assert "IAC_SCAN" in diff_v3["impact_analysis"]["required_validations"]
        results["step3_full_security_and_iac_scan"] = (
            f"change impact = HIGH, required_validations="
            f"{diff_v3['impact_analysis']['required_validations']}"
        )

        policy_v3 = await asyncio.to_thread(
            _run_simulated_pipeline,
            agent_id=agent_id,
            tenant_id=TENANT,
            version=3,
            deployment_id=deployment_id_v3,
            inject_critical=False,
        )
        assert policy_v3["result"] == "PASS"

        agent_v3 = client.get(f"/api/v1/agents/{agent_id}", headers=headers).json()["agent"]
        assert agent_v3["status"] == "ACTIVE"
        assert agent_v3["live_version"] == 3
        results["step3_final"] = "agent.status=ACTIVE, live_version=3"

        # ── Step 5: Inject critical security finding on a NEW attempt →
        # deployment BLOCKED → v3 remains ACTIVE ──────────────────────────
        # (Numbered per the spec's own step 5 — step 4 is a separate,
        # non-executable test below.) A no-op config edit is enough to
        # create v4 as the deploy target; the point under test is the
        # policy gate, not this edit's content.
        update_v4 = client.put(
            f"/api/v1/agents/{agent_id}",
            json={
                "configuration": jira_and_salesforce_config,
                "change_description": "Bump token budget",
            },
            headers=headers,
        )
        assert update_v4.status_code == 200
        assert update_v4.json()["version"] == 4

        deploy_v4_attempt = client.post(f"/api/v1/agents/{agent_id}/deploy", headers=headers)
        assert deploy_v4_attempt.status_code == 200
        deployment_id_v4 = deploy_v4_attempt.json()["deployment_id"]

        policy_v4 = await asyncio.to_thread(
            _run_simulated_pipeline,
            agent_id=agent_id,
            tenant_id=TENANT,
            version=4,
            deployment_id=deployment_id_v4,
            inject_critical=True,
        )
        assert policy_v4["result"] == "BLOCK"
        assert "critical_cve_found" in policy_v4["reason"] or "Critical" in policy_v4["reason"]

        agent_after_block = client.get(f"/api/v1/agents/{agent_id}", headers=headers).json()[
            "agent"
        ]
        deployment_v4 = await deployment_status_store.get_deployment(agent_id, deployment_id_v4)
        assert deployment_v4 is not None
        assert deployment_v4.status == "BLOCKED"
        # R22: previous version stays live — the AGENT reverts to ACTIVE
        # with live_version still 3, only the DEPLOYMENT record is BLOCKED.
        assert agent_after_block["status"] == "ACTIVE"
        assert agent_after_block["live_version"] == 3
        assert agent_after_block["current_version"] == 4
        results["step5_blocked"] = (
            f"deployment.status=BLOCKED (reason={policy_v4['reason']!r}), "
            f"agent.status=ACTIVE, live_version=3 (unchanged), "
            f"PR closed={len(fake_git.closed)}"
        )

        # ── Step 6: Rollback to v2 → v4... wait, rollback creates the NEXT
        # version number from v2's config → deploy → ACTIVE ─────────────
        # current_version is already 4 (the blocked attempt above, whose
        # config was never applied to any real infra — R03/F2). Rollback to
        # v2 therefore creates v5 here, carrying v2's exact configuration
        # (Jira only, original prompt edit, no Salesforce tool).
        rollback_response = client.post(
            f"/api/v1/agents/{agent_id}/rollback",
            json={
                "target_version": 2,
                "reason": "v4 introduced a critical CVE — rolling back to v2",
            },
            headers=headers,
        )
        assert rollback_response.status_code == 200, rollback_response.text
        rollback_body = rollback_response.json()
        assert rollback_body["version"] == 5
        assert rollback_body["rolled_back_from_version"] == 4
        deployment_id_v5 = rollback_body["deployment_id"]
        results["step6_rollback_created_version"] = (
            f"HTTP {rollback_response.status_code}, new version=5 "
            f"(rolled_back_from_version=4, carries v2's config)"
        )

        v5_detail = client.get(f"/api/v1/agents/{agent_id}/versions/5", headers=headers).json()
        assert v5_detail["configuration"]["system_prompt"] == edited_prompt_config["system_prompt"]
        assert [t["tool_id"] for t in v5_detail["configuration"]["tools"]] == ["jira"]

        policy_v5 = await asyncio.to_thread(
            _run_simulated_pipeline,
            agent_id=agent_id,
            tenant_id=TENANT,
            version=5,
            deployment_id=deployment_id_v5,
            inject_critical=False,
        )
        assert policy_v5["result"] == "PASS"

        agent_final = client.get(f"/api/v1/agents/{agent_id}", headers=headers).json()["agent"]
        assert agent_final["status"] == "ACTIVE"
        assert agent_final["live_version"] == 5
        results["step6_final"] = (
            "agent.status=ACTIVE, live_version=5 (v2's config, redeployed as v5), "
            f"PR merged total={len(fake_git.merged)}"
        )

    # Printed for `pytest -s` visibility; primarily the caller inspects
    # this test's pass/fail plus the asserted evidence above.
    for step, evidence in results.items():
        print(f"{step}: {evidence}")


def test_step4_terraform_drift_recreation_not_executable_in_this_environment() -> None:
    """Step 4 of the spec: "Manually delete a Lambda → edit agent →
    terraform plan detects missing → recreates → ACTIVE."

    This cannot be executed — not because of a missing test, but because
    the thing being tested doesn't exist as Panasa-owned code to invoke:

    1. No Terraform CLI is installed in this environment (`terraform
       version` -> "command not found") and no real/emulated AWS account
       backs any deployment this Runtime has ever triggered — every
       "Applying"/terraform-apply stage in this test suite (see
       test_phase17_full_lifecycle_e2e) is a DynamoDB stage-result write
       standing in for a customer CI/CD job, never a real `terraform
       apply`. There is therefore no real Lambda function anywhere for a
       "manually delete a Lambda" step to act on.

    2. This is not an incidental gap — it's the architecture (CLAUDE.md F0/
       F2/R03): "Panasa Runtime never reads customer Terraform state" and
       "terraform init/refresh/plan/apply... [is] solely the customer
       CI/CD's [job]". Drift detection is an emergent property of Terraform
       itself reconciling its state file against real cloud resources when
       the CUSTOMER runs `terraform plan` — it is not a Panasa-side module.
       (Confirmed by grep: there is no drift_validator.py or equivalent
       anywhere under app/modules/iac_generator/ in this codebase.)

    What IS verified, as the closest available evidence: editing an agent's
    tool configuration after a deploy goes through the exact same
    edit -> new version -> regenerate Terraform -> new deployment path as
    every other step in this scenario (steps 2/3/5 above) — the Terraform
    resource address for a tool Lambda is stable across versions
    (app/modules/iac_generator/templates/terraform/tools/tools.tf.j2 keys
    it by tool_id, not by version or deployment_id), so a real `terraform
    apply` against that regenerated configuration would, by Terraform's own
    ordinary behaviour, recreate any resource address present in the
    desired state but absent from live infrastructure. Nothing about that
    recreation depends on code this Runtime would need to implement.
    """
    import re
    from pathlib import Path

    tools_template = Path(
        "app/modules/iac_generator/templates/terraform/tools/tools.tf.j2"
    ).read_text()
    # The resource address is derived from tool_id alone — stable across
    # versions/deployments, which is what lets a real `terraform apply`
    # recreate a drifted-away resource without any Panasa-side involvement.
    assert re.search(r'resource\s+"aws_lambda_function"\s+"tool_', tools_template)
    assert "tool.tool_id" in tools_template
