"""Deterministic evaluation gate (CLAUDE.md F4 / R13 / R14).

Mirrors app.modules.security.policy_gate exactly in spirit: a pure function,
no I/O, no ML, no score that can override a Phase 1 metric falling below its
threshold. The caller (the POLICY_CHECK stage — see
app.modules.security.policy_enforcement.enforce_policy_gate) is responsible
for persisting the decision.
"""

from __future__ import annotations

from app.modules.evaluation.models import RAGAS_METRICS, EvaluationGateResult


def should_run_ragas(kb_enabled: bool, test_dataset_exists: bool) -> bool:
    """F4: RAGAS runs only when the agent has an enabled KB *and* a test
    dataset exists for it. Otherwise there is nothing for RAGAS to score."""
    return kb_enabled and test_dataset_exists


def evaluation_gate(scores: dict[str, float]) -> EvaluationGateResult:
    """Checks RAGAS scores against F4's thresholds.

    Only Phase 1 metrics (`blocks_deployment=True`) can block. Phase 2
    metrics are recorded in `scores` like any other metric but are always
    advisory — a low Phase 2 score is never a reason to block (R13).
    A metric absent from `scores` was never measured and cannot fail.
    """
    failed = [
        name
        for name, config in RAGAS_METRICS.items()
        if config.blocks_deployment and name in scores and scores[name] < config.threshold
    ]
    if failed:
        return EvaluationGateResult(
            decision="BLOCK",
            reason=f"RAGAS metric(s) below threshold: {', '.join(failed)}",
            failed_metrics=failed,
        )
    return EvaluationGateResult(decision="PASS", reason="All evaluation gates passed")
