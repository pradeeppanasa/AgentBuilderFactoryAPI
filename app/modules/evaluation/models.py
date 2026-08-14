"""Evaluation orchestration models (CLAUDE.md Section 28, Amendment A6, F4, Phase 10).

F4 supersedes both Section 28 and A6 and is the sole source of truth for
RAGAS metrics: 5 Phase 1 metrics block deployment, 2 Phase 2 metrics are
tracked but advisory-only until explicitly enabled. RAGAS itself only runs
for RAG agents with a test dataset (`should_run_ragas`, app.modules.
evaluation.gate) — the metrics below apply once scores exist, regardless of
where they came from.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class RagasMetricConfig(BaseModel):
    threshold: float
    blocks_deployment: bool


# F4 Phase 1 — implemented from day one, all five block deployment.
RAGAS_PHASE1_METRICS: dict[str, RagasMetricConfig] = {
    "faithfulness": RagasMetricConfig(threshold=0.70, blocks_deployment=True),
    "answer_relevancy": RagasMetricConfig(threshold=0.75, blocks_deployment=True),
    "context_precision": RagasMetricConfig(threshold=0.60, blocks_deployment=True),
    "context_recall": RagasMetricConfig(threshold=0.65, blocks_deployment=True),
    "noise_sensitivity": RagasMetricConfig(threshold=0.65, blocks_deployment=True),
}

# F4 Phase 2 — tracked once available, but never gate a deployment (advisory
# only) until explicitly enabled by configuration in a later phase.
RAGAS_PHASE2_METRICS: dict[str, RagasMetricConfig] = {
    "groundedness": RagasMetricConfig(threshold=0.75, blocks_deployment=False),
    "citation_accuracy": RagasMetricConfig(threshold=0.70, blocks_deployment=False),
}

RAGAS_METRICS: dict[str, RagasMetricConfig] = {**RAGAS_PHASE1_METRICS, **RAGAS_PHASE2_METRICS}


class EvaluationGateResult(BaseModel):
    decision: Literal["PASS", "BLOCK"]
    reason: str
    failed_metrics: list[str] = Field(default_factory=list)
