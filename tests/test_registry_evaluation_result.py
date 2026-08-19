"""AgentRegistryStore.record_evaluation_result (Phase 10, Section 4.2).

Exercises the versioner's JSON round-trip for the evaluation_result field —
the same _JSON_FIELDS path record_iac_artifact's scalar fields never touch,
so this is the regression test for record_derived_fields JSON-encoding
blob fields consistently with _to_item/_from_item.
"""

from __future__ import annotations

import boto3
import pytest

from app.config import settings
from app.modules.registry.models import AgentConfiguration, EvaluationResult
from app.modules.registry.store import AgentRegistryStore
from app.shared.exceptions import AgentNotFoundError, VersionNotFoundError


@pytest.fixture
async def registry_store() -> AgentRegistryStore:
    dynamodb = boto3.resource("dynamodb", region_name="eu-west-2")
    store = AgentRegistryStore(dynamodb, settings)
    await store.ensure_tables()
    return store


async def _seed_agent(registry_store: AgentRegistryStore) -> str:
    record, _version = await registry_store.create_agent(
        tenant_id="tenant-a",
        name="RAG Agent",
        description="desc",
        business_purpose="purpose",
        agent_type="standard",
        configuration=AgentConfiguration(
            model_id="anthropic.claude-3-5-sonnet-20241022-v2:0",
            model_provider="bedrock",
            system_prompt="You are a RAG agent.",
        ),
        created_by="dev@example.com",
    )
    return record.agent_id


async def test_record_evaluation_result_persists_and_round_trips(
    registry_store: AgentRegistryStore,
) -> None:
    agent_id = await _seed_agent(registry_store)

    result = EvaluationResult(
        passed=False,
        summary="RAGAS: faithfulness below threshold",
        scores={"faithfulness": 0.10, "context_recall": 0.91},
    )
    await registry_store.record_evaluation_result(
        tenant_id="tenant-a", agent_id=agent_id, version=1, evaluation_result=result
    )

    version = await registry_store.get_version_detail(
        tenant_id="tenant-a", agent_id=agent_id, version=1
    )
    assert version is not None
    assert version.evaluation_result is not None
    assert version.evaluation_result.passed is False
    assert version.evaluation_result.summary == "RAGAS: faithfulness below threshold"
    assert version.evaluation_result.scores == {"faithfulness": 0.10, "context_recall": 0.91}

    # The immutable configuration snapshot must survive untouched.
    assert version.configuration.system_prompt == "You are a RAG agent."


async def test_record_evaluation_result_unknown_agent_raises(
    registry_store: AgentRegistryStore,
) -> None:
    with pytest.raises(AgentNotFoundError):
        await registry_store.record_evaluation_result(
            tenant_id="tenant-a",
            agent_id="does-not-exist",
            version=1,
            evaluation_result=EvaluationResult(passed=True, summary="ok"),
        )


async def test_record_evaluation_result_unknown_version_raises(
    registry_store: AgentRegistryStore,
) -> None:
    agent_id = await _seed_agent(registry_store)

    with pytest.raises(VersionNotFoundError):
        await registry_store.record_evaluation_result(
            tenant_id="tenant-a",
            agent_id=agent_id,
            version=99,
            evaluation_result=EvaluationResult(passed=True, summary="ok"),
        )
