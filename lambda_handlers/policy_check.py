"""POLICY_CHECK stage (CLAUDE.md F1/F5/R06/R07) — the single automated
PASS/BLOCK decision point, reading the SECURITY_SCANNING and EVALUATING
stage results the CodeBuild jobs already wrote (F1: "reads all prior stage
results, no human input"). On PASS: merges the PR generating_iac.py opened.
On BLOCK: closes it instead (F5).

KNOWN GAP, inherited from Phase 9/10 and not fixed here (see
bootstrap/README.md): the 5 SecurityScanning branches and the CodeBuild
buildspecs generally only persist a per-stage status/summary/blocking_issue
string (codebuild/scripts/write_stage_result.sh) — there is no structured,
race-free list of SecurityFinding objects or RAGAS scores anywhere in
DynamoDB for this handler to read. What follows reconstructs a best-effort
SecurityFinding from SECURITY_SCANNING's blocking_issue (reliable in
practice only because every buildspec this bootstrap wrote sets that field
to one of CRITICAL_BLOCK_CONDITIONS's exact category strings on failure —
see codebuild/*-buildspec.yml) and does NOT attempt to reconstruct RAGAS
scores at all, since evaluation-buildspec.yml is itself a placeholder with
no real scoring yet. The stages["SECURITY_SCANNING"] key is also a single
shared key across 5 concurrent writers — a genuine, pre-existing race
(app/modules/deployment/status_store.py's read-modify-write is documented
safe only for strictly sequential stages, which SecurityScanning's parallel
branches are not). Fixing this properly needs `stages` to become a native
DynamoDB Map (atomic nested SET) instead of a single JSON-string attribute.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from app.modules.audit.writer import AuditEvent
from app.modules.security.models import SecurityFinding
from app.modules.security.policy_enforcement import enforce_policy_gate
from lambda_handlers.common import (
    audit_writer,
    deployment_status_store,
    git_provider,
    metrics_emitter,
    registry_store,
    require,
    require_git_repo_url,
    run,
)


async def _decide(agent_id: str, tenant_id: str, deployment_id: str, pr_id: str) -> dict[str, Any]:
    deployment = await deployment_status_store.get_deployment(agent_id, deployment_id)
    if deployment is None:
        raise RuntimeError(f"Deployment {deployment_id!r} not found for agent {agent_id!r}")

    evaluating = deployment.stages.get("EVALUATING")
    if evaluating is not None and evaluating.status == "FAILED":
        reason = evaluating.blocking_issue or evaluating.output_summary or "Evaluation failed"
        result_decision = "BLOCK"
        result_reason = reason
        await deployment_status_store.update_stage(
            agent_id=agent_id,
            deployment_id=deployment_id,
            stage="POLICY_CHECK",
            stage_status="BLOCKED",
            output_summary=reason,
            blocking_issue=reason,
            overall_status="BLOCKED",
            failure_reason=reason,
            failed_stage="POLICY_CHECK",
        )
        await registry_store.mark_deployment_blocked(
            tenant_id=tenant_id, agent_id=agent_id, updated_by="step-functions"
        )
        await audit_writer.write(
            AuditEvent(
                event_type="block",
                tenant_id=tenant_id,
                agent_id=agent_id,
                actor="step-functions",
                summary=f"Deployment {deployment_id} blocked: {reason}",
                occurred_at=datetime.now(UTC).isoformat(),
            )
        )
        await metrics_emitter.emit("DeploymentBlocked", dimensions={"tenant_id": tenant_id})
    else:
        security = deployment.stages.get("SECURITY_SCANNING")
        findings: list[SecurityFinding] = []
        if security is not None and security.status == "FAILED" and security.blocking_issue:
            findings.append(
                SecurityFinding(
                    scan_type="sast",  # best-effort — see module docstring
                    severity="CRITICAL",
                    category=security.blocking_issue,
                    description=security.output_summary or security.blocking_issue,
                )
            )

        gate_result = await enforce_policy_gate(
            findings,
            tenant_id=tenant_id,
            agent_id=agent_id,
            deployment_id=deployment_id,
            updated_by="step-functions",
            deployment_status_store=deployment_status_store,
            registry_store=registry_store,
            audit_writer=audit_writer,
            metrics_emitter=metrics_emitter,
        )
        result_decision = gate_result.decision
        result_reason = gate_result.reason

    git_repo_url = require_git_repo_url()
    if result_decision == "BLOCK":
        await git_provider.close_pull_request(git_repo_url, pr_id, reason=result_reason)
    else:
        await git_provider.merge_pull_request(git_repo_url, pr_id)

    return {"result": result_decision, "reason": result_reason}


def handler(event: dict[str, Any], _context: Any) -> dict[str, Any]:
    agent_id, tenant_id = require(event, "agentId", "tenantId")
    deployment_id = event["deploymentId"]
    pr_id = event["generatingIac"]["prId"]

    return run(_decide(agent_id, tenant_id, deployment_id, pr_id))
