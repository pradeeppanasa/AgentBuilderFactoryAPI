"""Deployment status models (CLAUDE.md Section 4.4, Section 5.3, F1, Section 45).

F1 (ARCHITECTURE FREEZE v1.2) established POLICY_CHECK as the only decision
point, fully automated — PASS proceeds to APPLYING, BLOCK stops the
pipeline. Section 45's R50/Stage 5 (2026-08-22) asked for a human-approval
gate before apply, which as written would have reversed F1 outright; the
resolution (Section 45's own "Resolution note") is that this is
**configurable, not absolute** — `approval_mode` on each DeploymentRecord
records which pipeline that specific deployment runs:

  "automated" (default) — F1 unchanged. PENDING_APPROVAL is never entered;
                           POLICY_CHECK PASS/BLOCK is the only decision.
  "manual"              — R50/Stage 5. POLICY_CHECK PASS parks the
                           deployment at PENDING_APPROVAL instead of
                           proceeding to APPLYING; APPLYING only starts
                           after POST .../approve (app/api/v1/agents.py).

R03/F2: this Runtime never runs terraform and never reaches these stages
itself. The customer-side CI/CD (Step Functions + CodeBuild, or one of
Section 45.6's generated provider workflows) writes stage results directly
to this table as it progresses; the Runtime only creates the initial
PENDING record when a deployment is triggered and exposes a read path over
what the CI/CD wrote. In "manual" mode, the CI/CD's own generated workflow
is responsible for checking `approval_mode` on reaching POLICY_CHECK=PASS
and parking at PENDING_APPROVAL (writing that stage_status itself) instead
of auto-continuing — the Runtime cannot reach into a customer's CI/CD job
to pause it (R57).
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

ApprovalMode = Literal["automated", "manual"]

# Section 45.6/R58 — which CI/CD workflow file iac_generator/cicd_templates.py
# generates for a tenant's per-agent repos (Section 45.2). One choice per
# tenant, stored on PlatformSettingsRecord — every agent's repo gets the
# same provider's workflow file.
CICDProvider = Literal["github_actions", "gitlab_ci", "azure_devops", "codebuild", "bitbucket"]

DeploymentStageName = Literal[
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
]

DEPLOYMENT_STAGE_ORDER: list[str] = [
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
]

DeploymentStatus = Literal[
    "PENDING",
    "VALIDATING",
    "CHANGE_IMPACT",
    "GENERATING_IAC",
    "SECURITY_SCANNING",
    "EVALUATING",
    "TERRAFORM_VALIDATE",
    "TERRAFORM_PLAN",
    "POLICY_CHECK",
    # Section 45.4 — manual-approval-mode only (see module docstring). Never
    # entered when approval_mode == "automated".
    "PENDING_APPROVAL",
    "APPLYING",
    "DEPLOYING",
    "HEALTH_CHECK",
    "ACTIVE",
    "FAILED",
    "BLOCKED",
]

StageStatus = Literal["PENDING", "RUNNING", "PASSED", "FAILED", "SKIPPED", "BLOCKED"]


class StageResult(BaseModel):
    stage: str
    status: StageStatus = "PENDING"
    started_at: str | None = None
    completed_at: str | None = None
    output_summary: str | None = None
    """Human-readable, never raw secrets or payloads (Section 4.4)."""
    blocking_issue: str | None = None


class DeploymentRecord(BaseModel):
    agent_id: str
    deployment_id: str
    version: int
    triggered_by: str
    triggered_at: str

    status: DeploymentStatus = "PENDING"
    current_stage: str = DEPLOYMENT_STAGE_ORDER[0]

    approval_mode: ApprovalMode = "automated"
    approved_by: str | None = None
    """Set only when a human calls POST .../approve on a "manual"-mode
    deployment parked at PENDING_APPROVAL."""
    approved_at: str | None = None

    stages: dict[str, StageResult] = Field(default_factory=dict)

    terraform_plan_summary: str | None = None
    terraform_apply_output: str | None = None
    iac_s3_key: str | None = None

    # Section 45.2/45.3 — where the IaC that iac_s3_key points to actually
    # landed. branch is the per-agent repo's default branch for a v1 (repo
    # didn't exist yet) deploy, or the throwaway deploy/v{N}-{deployment_id}
    # branch for v2+; pull_request_id is None for the v1 case (no PR is ever
    # opened for a direct-to-default-branch push) — lambda_handlers/
    # policy_check.py skips the merge/close step whenever it's None.
    branch: str | None = None
    pull_request_id: str | None = None

    health_check_url: str | None = None
    health_check_passed: bool | None = None

    failure_reason: str | None = None
    failed_stage: str | None = None

    updated_at: str


def initial_stages() -> dict[str, StageResult]:
    return {stage: StageResult(stage=stage, status="PENDING") for stage in DEPLOYMENT_STAGE_ORDER}
