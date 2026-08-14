"""Unit tests for app.modules.evaluation.gate (CLAUDE.md F4 / R13 / R14)."""

from app.modules.evaluation.gate import evaluation_gate, should_run_ragas
from app.modules.evaluation.models import RAGAS_PHASE1_METRICS, RAGAS_PHASE2_METRICS


def _passing_scores() -> dict[str, float]:
    return {name: config.threshold for name, config in RAGAS_PHASE1_METRICS.items()}


# ── should_run_ragas (F4) ────────────────────────────────────────────────────


def test_should_run_ragas_requires_both_kb_and_dataset() -> None:
    assert should_run_ragas(kb_enabled=True, test_dataset_exists=True) is True
    assert should_run_ragas(kb_enabled=True, test_dataset_exists=False) is False
    assert should_run_ragas(kb_enabled=False, test_dataset_exists=True) is False
    assert should_run_ragas(kb_enabled=False, test_dataset_exists=False) is False


# ── evaluation_gate (F4 Phase 1 metrics table) ───────────────────────────────


def test_phase1_metrics_table_matches_spec() -> None:
    assert {name: config.threshold for name, config in RAGAS_PHASE1_METRICS.items()} == {
        "faithfulness": 0.70,
        "answer_relevancy": 0.75,
        "context_precision": 0.60,
        "context_recall": 0.65,
        "noise_sensitivity": 0.65,
    }
    assert all(config.blocks_deployment for config in RAGAS_PHASE1_METRICS.values())


def test_phase2_metrics_are_advisory_only() -> None:
    assert {name: config.threshold for name, config in RAGAS_PHASE2_METRICS.items()} == {
        "groundedness": 0.75,
        "citation_accuracy": 0.70,
    }
    assert not any(config.blocks_deployment for config in RAGAS_PHASE2_METRICS.values())


def test_all_scores_at_threshold_passes() -> None:
    result = evaluation_gate(_passing_scores())
    assert result.decision == "PASS"
    assert result.failed_metrics == []


def test_missing_scores_never_fail() -> None:
    # A metric that was never measured can't fail the gate.
    result = evaluation_gate({})
    assert result.decision == "PASS"


def test_single_phase1_metric_below_threshold_blocks() -> None:
    scores = _passing_scores()
    scores["faithfulness"] = 0.69
    result = evaluation_gate(scores)
    assert result.decision == "BLOCK"
    assert result.failed_metrics == ["faithfulness"]
    assert "faithfulness" in result.reason


def test_multiple_phase1_metrics_below_threshold_all_reported() -> None:
    scores = _passing_scores()
    scores["context_precision"] = 0.10
    scores["context_recall"] = 0.20
    result = evaluation_gate(scores)
    assert result.decision == "BLOCK"
    assert set(result.failed_metrics) == {"context_precision", "context_recall"}


def test_phase2_metric_below_threshold_never_blocks() -> None:
    scores = _passing_scores()
    scores["groundedness"] = 0.01
    scores["citation_accuracy"] = 0.01
    result = evaluation_gate(scores)
    assert result.decision == "PASS"


def test_phase2_failure_alongside_phase1_failure_only_reports_phase1() -> None:
    scores = _passing_scores()
    scores["faithfulness"] = 0.01
    scores["groundedness"] = 0.01
    result = evaluation_gate(scores)
    assert result.decision == "BLOCK"
    assert result.failed_metrics == ["faithfulness"]
