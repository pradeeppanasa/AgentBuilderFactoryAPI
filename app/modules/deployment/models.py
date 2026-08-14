"""Deployment status models (CLAUDE.md Section 4.4, Section 5.3, F1).

F1 (ARCHITECTURE FREEZE v1.2) supersedes the earlier 11-stage list
(Section 6.2) and the 13-stage PLAN_REVIEW variant (A11/A12): there is no
manual approval gate. POLICY_CHECK is the only decision point, and it is
fully automated — PASS proceeds to APPLYING, BLOCK stops the pipeline.

R03/F2: this Runtime never runs terraform and never reaches these stages
itself. The customer-side CI/CD (Step Functions + CodeBuild) writes stage
results directly to this table as it progresses; the Runtime only creates
the initial PENDING record when a deployment is triggered and exposes a
read path over what the CI/CD wrote.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

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

    stages: dict[str, StageResult] = Field(default_factory=dict)

    terraform_plan_summary: str | None = None
    terraform_apply_output: str | None = None
    iac_s3_key: str | None = None

    health_check_url: str | None = None
    health_check_passed: bool | None = None

    failure_reason: str | None = None
    failed_stage: str | None = None

    updated_at: str


def initial_stages() -> dict[str, StageResult]:
    return {stage: StageResult(stage=stage, status="PENDING") for stage in DEPLOYMENT_STAGE_ORDER}
