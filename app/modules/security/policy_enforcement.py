"""Ties policy_gate's PASS/BLOCK decision to persistence (Phase 9, extended Phase 10).

Called by the POLICY_CHECK stage (step_functions/deployment_workflow.json's
${policy_check_lambda_arn}) once results from the SecurityScanning and
Evaluating stages have been gathered — per that step's own comment, PolicyCheck
"reads all prior stage results" and makes the single automated PASS/BLOCK
decision (F1/R06/R07). On BLOCK: the deployment's status becomes BLOCKED and
the previous version remains LIVE — literally Phase 9's deliverable
("Critical finding → deployment status = BLOCKED, previous version remains
LIVE"), extended in Phase 10 to also cover a RAGAS metric falling below
threshold (F4/R13 — "ragas_metric_below_threshold" in F1's
PRODUCTION_READINESS_BLOCKERS).

`evaluation_scores` is optional and defaults to None (no evaluation ran, or
EVALUATING was SKIPPED per R14) — the security-only Phase 9 call sites are
unaffected. Security findings are checked first: a CRITICAL security finding
always blocks regardless of evaluation scores, matching Section 7's "no ML,
no score that can override a critical finding".
"""

from __future__ import annotations

from datetime import UTC, datetime

from app.modules.audit.writer import AuditEvent, AuditWriter
from app.modules.deployment.status_store import DeploymentStatusStore
from app.modules.evaluation.gate import evaluation_gate
from app.modules.observability.metrics import MetricsEmitter
from app.modules.registry.store import AgentRegistryStore
from app.modules.security.models import PolicyGateResult, SecurityFinding
from app.modules.security.policy_gate import policy_gate


async def enforce_policy_gate(
    findings: list[SecurityFinding],
    *,
    tenant_id: str,
    agent_id: str,
    deployment_id: str,
    updated_by: str,
    deployment_status_store: DeploymentStatusStore,
    registry_store: AgentRegistryStore,
    evaluation_scores: dict[str, float] | None = None,
    audit_writer: AuditWriter | None = None,
    metrics_emitter: MetricsEmitter | None = None,
) -> PolicyGateResult:
    """audit_writer/metrics_emitter (Phase 14) are optional, defaulting to
    None — every call site that predates Phase 14 (this function's own
    tests, lambda_handlers/policy_check.py) passes neither and is
    unaffected; the "block" audit event + DeploymentBlocked metric only
    fire when a caller opts in by supplying both."""
    result = policy_gate(findings)

    if result.decision == "PASS" and evaluation_scores is not None:
        eval_result = evaluation_gate(evaluation_scores)
        if eval_result.decision == "BLOCK":
            result = PolicyGateResult(decision="BLOCK", reason=eval_result.reason)

    if result.decision == "BLOCK" and audit_writer is not None and metrics_emitter is not None:
        await audit_writer.write(
            AuditEvent(
                event_type="block",
                tenant_id=tenant_id,
                agent_id=agent_id,
                actor=updated_by,
                summary=f"Deployment {deployment_id} blocked: {result.reason}",
                occurred_at=datetime.now(UTC).isoformat(),
            )
        )
        await metrics_emitter.emit("DeploymentBlocked", dimensions={"tenant_id": tenant_id})

    if result.decision == "BLOCK":
        await deployment_status_store.update_stage(
            agent_id=agent_id,
            deployment_id=deployment_id,
            stage="POLICY_CHECK",
            stage_status="BLOCKED",
            output_summary=result.reason,
            blocking_issue=result.reason,
            overall_status="BLOCKED",
            failure_reason=result.reason,
            failed_stage="POLICY_CHECK",
        )
        await registry_store.mark_deployment_blocked(
            tenant_id=tenant_id, agent_id=agent_id, updated_by=updated_by
        )
    else:
        await deployment_status_store.update_stage(
            agent_id=agent_id,
            deployment_id=deployment_id,
            stage="POLICY_CHECK",
            stage_status="PASSED",
            output_summary=result.reason,
            overall_status="APPLYING",
        )

    return result
