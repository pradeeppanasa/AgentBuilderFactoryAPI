"""Local-dev-only deployment pipeline simulator.

In a real deployment, the customer's own CI/CD writes every stage result
into panasa-deployments directly — the Runtime never touches these stages
itself (R03/F2, see this package's models.py docstring). That CI/CD
doesn't exist in local dev: there's no bootstrapped Step Functions/
CodeBuild pipeline, and the generated per-agent GitHub Actions workflow
(Section 45.6) has no path back to a laptop's DynamoDB Local. Without
either, the Console's deployment status page sits at PENDING forever.

This simulator stands in for that missing pipeline so the deployment
status UI can be exercised end-to-end locally. It is gated behind
Settings.simulate_deployment_pipeline (default False) and must never be
enabled against a real customer environment.

VALIDATING/CHANGE_IMPACT/GENERATING_IAC/EVALUATING/TERRAFORM_PLAN/APPLYING/
DEPLOYING/HEALTH_CHECK are always fabricated — real terraform plan/apply
needs IaC Generator fixes (root module composition, unresolved required
variables) that are a separate, larger piece of work.

SECURITY_SCANNING/TERRAFORM_VALIDATE/POLICY_CHECK are REAL whenever an
IaCScanRunner and the generated IaC files are supplied (see
iac_scan_runner.py) — genuine tfsec/checkov findings and a genuine
policy_gate() PASS/BLOCK decision, not fabricated text. Falls back to
fabricated PASSED text for these three too when either is omitted, so
existing callers/tests that only care about the fake-everything path
keep working unchanged.

Mirrors two real pipeline steps' *logic* (not their code — those live in
Lambda handlers that only run inside a bootstrapped Step Functions
pipeline):
  - lambda_handlers/policy_check.py: merge the PR on POLICY_CHECK PASS,
    close it on BLOCK (skipped entirely for a v1 deploy, which has no PR
    — pull_request_id is None).
  - lambda_handlers/mark_active.py: HEALTH_CHECK PASSED and the agent
    becoming ACTIVE/live_version happen together.
"""

from __future__ import annotations

import asyncio
from typing import Any

from aws_xray_sdk.core import xray_recorder

from app.config import Settings
from app.modules.deployment.iac_scan_runner import IaCScanRunner
from app.modules.deployment.models import DEPLOYMENT_STAGE_ORDER, DeploymentRecord
from app.modules.deployment.status_store import DeploymentStatusStore
from app.modules.git_provider._util import agent_repo_identifier
from app.modules.registry.models import AgentConfiguration
from app.modules.registry.store import AgentRegistryStore
from app.modules.security.models import SecurityScanSummary
from app.shared.logging import get_logger

log = get_logger()

_PRE_SCAN_STAGES = ["VALIDATING", "CHANGE_IMPACT", "GENERATING_IAC"]
_POST_SCAN_FAKE_STAGES = ["EVALUATING", "TERRAFORM_PLAN"]
_STAGES_APPLYING_ONWARD = ["APPLYING", "DEPLOYING"]  # HEALTH_CHECK is paired with ACTIVE below

_SEVERITY_SORT_ORDER = {"CRITICAL": 0, "HIGH": 1, "MEDIUM": 2, "LOW": 3}
_SECURITY_FINDINGS_OUTPUT_CHAR_LIMIT = 4000
"""Real tfsec/checkov output can run to dozens of findings — worst
(CRITICAL/HIGH) first, matching StageResult.output_summary's own
"human-readable, never raw payloads" rule (Section 4.4): every field here
already comes from SecurityFinding (severity/category/description/
location), never raw tool stdout."""


def _format_security_findings(summary: SecurityScanSummary) -> str:
    """The one-line summary() alone (e.g. "tfsec + checkov: 1 critical, 7
    high, ...") used to be the entire output_summary — the per-finding
    detail was computed by IaCScanRunner but then discarded, with no way
    for a user to see anything beyond the aggregate counts already shown
    in the Console's stage tracker. This is the fix: the same detail
    TERRAFORM_VALIDATE's own output_summary already shows per-check."""
    if not summary.findings:
        return summary.summary

    sorted_findings = sorted(
        summary.findings, key=lambda f: _SEVERITY_SORT_ORDER.get(f.severity, 4)
    )
    lines = [summary.summary, ""]
    for finding in sorted_findings:
        location = f" ({finding.location})" if finding.location else ""
        lines.append(f"{finding.severity:<8} {finding.category}: {finding.description}{location}")
    return "\n".join(lines)[:_SECURITY_FINDINGS_OUTPUT_CHAR_LIMIT]

assert DEPLOYMENT_STAGE_ORDER == [
    "VALIDATING",
    "CHANGE_IMPACT",
    "GENERATING_IAC",
    "SECURITY_SCANNING",
    "EVALUATING",
    "TERRAFORM_VALIDATE",
    "TERRAFORM_PLAN",
    "POLICY_CHECK",
    "APPLYING",
    "DEPLOYING",
    "HEALTH_CHECK",
], "pipeline_simulator's hardcoded stage groupings assume this exact order"


def _detach_from_request_xray_segment() -> None:
    """run()/resume_after_approval() are FastAPI BackgroundTasks — they
    execute after the triggering request's own response has been sent, by
    which point app/middleware/xray.py's `finally: xray_recorder.
    end_segment()` may already have run for that request. AsyncContext
    propagates contextvars into the background task regardless, so without
    this it doesn't see "no segment" (which app/main.py's
    context_missing="LOG_ERROR" already handles gracefully) — it sees a
    REAL but already-ended segment reference, and the first boto3 call
    inside it (patch(["boto3"]) instruments every one) raises
    aws_xray_sdk.core.exceptions.exceptions.AlreadyEndedException,
    aborting the whole background run. Reproduced live: every deployment
    sat at PENDING forever, `deployment_pipeline_simulator.run.failed` in
    the logs on literally the first DynamoDB call. clear_trace_entities()
    drops that stale reference so subsequent boto3 calls correctly hit the
    already-handled "no segment" path instead."""
    xray_recorder.clear_trace_entities()


class DeploymentPipelineSimulator:
    def __init__(
        self,
        deployment_status_store: DeploymentStatusStore,
        registry_store: AgentRegistryStore,
        git_provider: Any,
        settings: Settings,
        stage_delay_seconds: float = 1.0,
        iac_scan_runner: IaCScanRunner | None = None,
    ) -> None:
        self._deployments = deployment_status_store
        self._registry = registry_store
        self._git = git_provider
        self._settings = settings
        self._stage_delay_seconds = stage_delay_seconds
        self._scan_runner = iac_scan_runner

    async def run(
        self,
        tenant_id: str,
        agent_id: str,
        deployment_id: str,
        config: AgentConfiguration | None = None,
        iac_files: dict[str, str] | None = None,
    ) -> None:
        """Entry point for a freshly triggered deployment — runs from
        VALIDATING through POLICY_CHECK, then either continues straight to
        ACTIVE ("automated" mode) or parks at PENDING_APPROVAL ("manual"
        mode, resumed later by resume_after_approval).

        config/iac_files enable the real SECURITY_SCANNING/
        TERRAFORM_VALIDATE/POLICY_CHECK path (see module docstring); either
        omitted falls back to fabricating all three like every other stage.
        """
        _detach_from_request_xray_segment()
        try:
            record = await self._deployments.get_deployment(agent_id, deployment_id)
            if record is None:
                return

            await self._advance_stages(agent_id, deployment_id, _PRE_SCAN_STAGES)

            policy_passed = await self._run_scan_and_policy_check(
                agent_id, tenant_id, record, config, iac_files
            )
            if not policy_passed:
                return  # already marked BLOCKED/closed the PR

            if record.approval_mode == "manual":
                await self._deployments.update_stage(
                    agent_id,
                    deployment_id,
                    stage="POLICY_CHECK",
                    stage_status="PASSED",
                    overall_status="PENDING_APPROVAL",
                )
                return

            await self._finish(tenant_id, agent_id, deployment_id, record)
        except Exception:
            log.warning("deployment_pipeline_simulator.run.failed", exc_info=True)

    async def resume_after_approval(
        self, tenant_id: str, agent_id: str, deployment_id: str
    ) -> None:
        """Entry point for a "manual"-mode deployment that was just approved
        via POST .../approve — continues from APPLYING onward."""
        _detach_from_request_xray_segment()
        try:
            record = await self._deployments.get_deployment(agent_id, deployment_id)
            if record is None:
                return
            await self._finish(tenant_id, agent_id, deployment_id, record)
        except Exception:
            log.warning("deployment_pipeline_simulator.resume.failed", exc_info=True)

    async def _run_scan_and_policy_check(
        self,
        agent_id: str,
        tenant_id: str,
        record: DeploymentRecord,
        config: AgentConfiguration | None,
        iac_files: dict[str, str] | None,
    ) -> bool:
        """Runs SECURITY_SCANNING, EVALUATING (fake), TERRAFORM_VALIDATE,
        TERRAFORM_PLAN (fake), POLICY_CHECK in that order. Returns whether
        POLICY_CHECK passed (False means the deployment has already been
        marked BLOCKED and the PR, if any, closed — the caller must stop)."""
        deployment_id = record.deployment_id

        if self._scan_runner is None or config is None or iac_files is None:
            await self._advance_stages(
                agent_id,
                deployment_id,
                ["SECURITY_SCANNING", "EVALUATING", "TERRAFORM_VALIDATE", "TERRAFORM_PLAN"],
            )
            await self._advance_stages(agent_id, deployment_id, ["POLICY_CHECK"])
            return True

        await self._deployments.update_stage(
            agent_id, deployment_id, stage="SECURITY_SCANNING", stage_status="RUNNING"
        )
        scan_result = await self._scan_runner.run(
            agent_id=agent_id,
            tenant_id=tenant_id,
            version=record.version,
            config=config,
            files=iac_files,
        )
        await self._deployments.update_stage(
            agent_id,
            deployment_id,
            stage="SECURITY_SCANNING",
            stage_status="PASSED",
            output_summary=_format_security_findings(scan_result.security_summary),
        )

        await self._advance_stages(agent_id, deployment_id, ["EVALUATING"])

        validation_lines = [
            f"{'PASS' if c.passed else 'FAIL'} {c.name}: {c.detail}"
            for c in scan_result.validation_report.checks
        ]
        await self._deployments.update_stage(
            agent_id,
            deployment_id,
            stage="TERRAFORM_VALIDATE",
            stage_status="PASSED" if scan_result.validation_report.passed else "FAILED",
            output_summary="\n".join(validation_lines)[:2000],
        )

        await self._advance_stages(agent_id, deployment_id, ["TERRAFORM_PLAN"])

        if scan_result.policy_decision == "BLOCK":
            await self._block(agent_id, tenant_id, record, scan_result.policy_reason)
            return False

        await self._deployments.update_stage(
            agent_id,
            deployment_id,
            stage="POLICY_CHECK",
            stage_status="PASSED",
            output_summary=scan_result.policy_reason,
        )
        return True

    async def _block(
        self, agent_id: str, tenant_id: str, record: DeploymentRecord, reason: str
    ) -> None:
        if record.pull_request_id is not None:
            repo = agent_repo_identifier(
                self._settings.git_provider, self._settings.git_org, agent_id
            )
            if repo is not None:
                await self._git.close_pull_request(repo, record.pull_request_id, reason)

        await self._registry.mark_deployment_blocked(
            tenant_id=tenant_id, agent_id=agent_id, updated_by="deployment-pipeline-simulator"
        )
        await self._deployments.update_stage(
            agent_id,
            record.deployment_id,
            stage="POLICY_CHECK",
            stage_status="BLOCKED",
            output_summary=reason,
            overall_status="BLOCKED",
            failure_reason=reason,
            failed_stage="POLICY_CHECK",
        )

    async def _finish(
        self, tenant_id: str, agent_id: str, deployment_id: str, record: DeploymentRecord
    ) -> None:
        if record.pull_request_id is not None:
            repo = agent_repo_identifier(
                self._settings.git_provider, self._settings.git_org, agent_id
            )
            if repo is not None:
                await self._git.merge_pull_request(repo, record.pull_request_id)

        await self._advance_stages(agent_id, deployment_id, _STAGES_APPLYING_ONWARD)

        await self._registry.mark_deployment_active(
            tenant_id=tenant_id,
            agent_id=agent_id,
            live_version=record.version,
            updated_by="deployment-pipeline-simulator",
        )
        await self._deployments.update_stage(
            agent_id,
            deployment_id,
            stage="HEALTH_CHECK",
            stage_status="PASSED",
            overall_status="ACTIVE",
            output_summary="[simulated] Health check passed.",
        )

    async def _advance_stages(
        self, agent_id: str, deployment_id: str, stages: list[str]
    ) -> None:
        for stage in stages:
            await self._deployments.update_stage(
                agent_id, deployment_id, stage=stage, stage_status="RUNNING"
            )
            await asyncio.sleep(self._stage_delay_seconds)
            await self._deployments.update_stage(
                agent_id,
                deployment_id,
                stage=stage,
                stage_status="PASSED",
                output_summary=f"[simulated] {stage.replace('_', ' ').title()} passed.",
            )
            await asyncio.sleep(self._stage_delay_seconds * 0.5)
