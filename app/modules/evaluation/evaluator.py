"""Evaluation orchestration (CLAUDE.md Phase 10 / Section 6.2 / F4 R14).

R05: the customer's CI/CD owns actual evaluation *execution* — a single
CodeBuild project runs RAGAS (RAG agents only) and the targeted agent
evaluation tests implied by Change Impact's `required_validations` (Section
7). This module only starts that already-provisioned CodeBuild project
(boto3 start_build) and reports back its build id; it never runs an
evaluation itself, and it never reads raw scores or transcripts (R15) — the
CodeBuild job is expected to write its own StageResult (via a
write_stage_result.sh-style update) and EvaluationResult (see
app.modules.registry.store.AgentRegistryStore.record_evaluation_result) on
completion, matching the security scanning module's division of
responsibility exactly.

R14, generalised: RAGAS is skipped when there's no KB + test dataset to
score. If there is *also* nothing else to evaluate (Change Impact produced
no `required_validations` for this change — e.g. a token_budget_daily-only
edit), there is no reason to start the CodeBuild job at all, so
`start_evaluation` returns None and the caller should mark the EVALUATING
stage SKIPPED directly, exactly as R14 describes.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from typing import Any

EVALUATION_CODEBUILD_PROJECT = "panasa-evaluation"


def codebuild_project_name() -> str:
    return EVALUATION_CODEBUILD_PROJECT


class AgentEvaluator:
    def __init__(self, codebuild_client: Any) -> None:
        self._codebuild = codebuild_client

    async def start_evaluation(
        self,
        agent_id: str,
        deployment_id: str,
        required_validations: Sequence[str] = (),
        run_ragas: bool = False,
    ) -> str | None:
        """Starts the evaluation CodeBuild project. Returns its build id, or
        None if there is nothing to evaluate (caller should mark EVALUATING
        as SKIPPED instead of RUNNING)."""

        if not required_validations and not run_ragas:
            return None

        response = await asyncio.to_thread(
            self._codebuild.start_build,
            projectName=codebuild_project_name(),
            environmentVariablesOverride=[
                {"name": "AGENT_ID", "value": agent_id, "type": "PLAINTEXT"},
                {"name": "DEPLOYMENT_ID", "value": deployment_id, "type": "PLAINTEXT"},
                {
                    "name": "REQUIRED_VALIDATIONS",
                    "value": ",".join(required_validations),
                    "type": "PLAINTEXT",
                },
                {
                    "name": "RUN_RAGAS",
                    "value": "true" if run_ragas else "false",
                    "type": "PLAINTEXT",
                },
            ],
        )
        build_id: str = response["build"]["id"]
        return build_id
