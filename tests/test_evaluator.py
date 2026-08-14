"""AgentEvaluator tests against moto's CodeBuild mock (Phase 10).

Mirrors tests/test_security_scanner.py's approach exactly, including the
recording-wrapper workaround for moto's start_build not echoing back
environmentVariablesOverride (see that file's module docstring).
"""

from __future__ import annotations

from typing import Any

import boto3
import pytest

from app.modules.evaluation.evaluator import AgentEvaluator, codebuild_project_name


class _RecordingCodeBuildClient:
    def __init__(self, real_client: Any) -> None:
        self._real = real_client
        self.start_build_calls: list[dict[str, Any]] = []

    def start_build(self, **kwargs: Any) -> Any:
        self.start_build_calls.append(kwargs)
        return self._real.start_build(**kwargs)


@pytest.fixture
def codebuild_client():
    client = boto3.client("codebuild", region_name="eu-west-2")
    client.create_project(
        name=codebuild_project_name(),
        source={"type": "GITHUB", "location": "https://github.com/example/repo.git"},
        artifacts={"type": "NO_ARTIFACTS"},
        environment={
            "type": "LINUX_CONTAINER",
            "image": "aws/codebuild/standard:7.0",
            "computeType": "BUILD_GENERAL1_SMALL",
        },
        serviceRole="arn:aws:iam::123456789012:role/service-role/test",
    )
    return _RecordingCodeBuildClient(client)


def test_codebuild_project_name_convention() -> None:
    assert codebuild_project_name() == "panasa-evaluation"


async def test_start_evaluation_returns_none_when_nothing_to_evaluate(codebuild_client) -> None:
    evaluator = AgentEvaluator(codebuild_client)

    build_id = await evaluator.start_evaluation("agent-1", "DEP-AAAAAAAA")

    assert build_id is None
    assert codebuild_client.start_build_calls == []


async def test_start_evaluation_runs_for_required_validations_without_ragas(
    codebuild_client,
) -> None:
    evaluator = AgentEvaluator(codebuild_client)

    build_id = await evaluator.start_evaluation(
        "agent-1", "DEP-AAAAAAAA", required_validations=["PROMPT_EVALUATION"]
    )

    assert build_id
    call = codebuild_client.start_build_calls[0]
    assert call["projectName"] == "panasa-evaluation"
    env_vars = {e["name"]: e["value"] for e in call["environmentVariablesOverride"]}
    assert env_vars == {
        "AGENT_ID": "agent-1",
        "DEPLOYMENT_ID": "DEP-AAAAAAAA",
        "REQUIRED_VALIDATIONS": "PROMPT_EVALUATION",
        "RUN_RAGAS": "false",
    }


async def test_start_evaluation_runs_for_ragas_alone(codebuild_client) -> None:
    evaluator = AgentEvaluator(codebuild_client)

    build_id = await evaluator.start_evaluation("agent-2", "DEP-BBBBBBBB", run_ragas=True)

    assert build_id
    call = codebuild_client.start_build_calls[0]
    env_vars = {e["name"]: e["value"] for e in call["environmentVariablesOverride"]}
    assert env_vars["RUN_RAGAS"] == "true"
    assert env_vars["REQUIRED_VALIDATIONS"] == ""


async def test_start_evaluation_joins_multiple_required_validations(codebuild_client) -> None:
    evaluator = AgentEvaluator(codebuild_client)

    await evaluator.start_evaluation(
        "agent-3",
        "DEP-CCCCCCCC",
        required_validations=["PROMPT_EVALUATION", "GUARDRAIL_TESTS"],
        run_ragas=True,
    )

    call = codebuild_client.start_build_calls[0]
    env_vars = {e["name"]: e["value"] for e in call["environmentVariablesOverride"]}
    assert env_vars["REQUIRED_VALIDATIONS"] == "PROMPT_EVALUATION,GUARDRAIL_TESTS"
    assert env_vars["RUN_RAGAS"] == "true"
