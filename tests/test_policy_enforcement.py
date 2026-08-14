"""enforce_policy_gate ties policy_gate's decision to both stores (Phase 9)."""

from __future__ import annotations

import boto3

from app.config import settings
from app.modules.deployment.models import DeploymentRecord, initial_stages
from app.modules.deployment.status_store import DeploymentStatusStore
from app.modules.registry.models import AgentConfiguration
from app.modules.registry.store import AgentRegistryStore
from app.modules.security.models import SecurityFinding
from app.modules.security.policy_enforcement import enforce_policy_gate

TENANT_ID = "tenant-a"


async def _make_stores() -> tuple[AgentRegistryStore, DeploymentStatusStore]:
    dynamodb = boto3.resource("dynamodb", region_name="eu-west-2")
    registry_store = AgentRegistryStore(dynamodb, settings)
    await registry_store.ensure_tables()
    deployment_status_store = DeploymentStatusStore(dynamodb, settings)
    await deployment_status_store.ensure_table()
    return registry_store, deployment_status_store


async def _seed_agent_and_deployment(
    registry_store: AgentRegistryStore,
    deployment_status_store: DeploymentStatusStore,
    deployment_id: str,
    *,
    live_version: int | None,
) -> str:
    record, _version = await registry_store.create_agent(
        tenant_id=TENANT_ID,
        name="KYC Agent",
        description="desc",
        business_purpose="purpose",
        agent_type="task",
        configuration=AgentConfiguration(
            model_id="anthropic.claude-3-5-sonnet-20241022-v2:0",
            model_provider="bedrock",
            system_prompt="You are a KYC agent.",
        ),
        created_by="dev@example.com",
    )

    if live_version is not None:
        agents_table = boto3.resource("dynamodb", region_name="eu-west-2").Table(
            settings.dynamodb_agents_table
        )
        live_record = record.model_copy(update={"live_version": live_version, "status": "ACTIVE"})
        agents_table.put_item(Item=live_record.model_dump(mode="json"))

    await deployment_status_store.create_deployment(
        DeploymentRecord(
            agent_id=record.agent_id,
            deployment_id=deployment_id,
            version=record.current_version,
            triggered_by="dev@example.com",
            triggered_at="2026-01-01T00:00:00+00:00",
            stages=initial_stages(),
            updated_at="2026-01-01T00:00:00+00:00",
        )
    )
    return record.agent_id


async def test_blocking_finding_marks_deployment_blocked_and_agent_has_no_live_version() -> None:
    registry_store, deployment_status_store = await _make_stores()
    agent_id = await _seed_agent_and_deployment(
        registry_store, deployment_status_store, "DEP-BLOCK-1", live_version=None
    )

    result = await enforce_policy_gate(
        [
            SecurityFinding(
                scan_type="secret_scan",
                severity="CRITICAL",
                category="hardcoded_secret_found",
                description="AWS key committed",
            )
        ],
        tenant_id=TENANT_ID,
        agent_id=agent_id,
        deployment_id="DEP-BLOCK-1",
        updated_by="dev@example.com",
        deployment_status_store=deployment_status_store,
        registry_store=registry_store,
    )

    assert result.decision == "BLOCK"

    deployment = await deployment_status_store.get_deployment(agent_id, "DEP-BLOCK-1")
    assert deployment is not None
    assert deployment.status == "BLOCKED"
    assert deployment.failed_stage == "POLICY_CHECK"
    assert deployment.stages["POLICY_CHECK"].status == "BLOCKED"

    agent = await registry_store.get_agent(TENANT_ID, agent_id)
    assert agent is not None
    assert agent.status == "BLOCKED"  # no prior live version to fall back to


async def test_blocking_finding_reverts_agent_to_active_when_a_version_was_live() -> None:
    registry_store, deployment_status_store = await _make_stores()
    agent_id = await _seed_agent_and_deployment(
        registry_store, deployment_status_store, "DEP-BLOCK-2", live_version=1
    )

    await enforce_policy_gate(
        [
            SecurityFinding(
                scan_type="iac_scan",
                severity="CRITICAL",
                category="iam_privilege_escalation",
                description="Wildcard IAM policy",
            )
        ],
        tenant_id=TENANT_ID,
        agent_id=agent_id,
        deployment_id="DEP-BLOCK-2",
        updated_by="dev@example.com",
        deployment_status_store=deployment_status_store,
        registry_store=registry_store,
    )

    agent = await registry_store.get_agent(TENANT_ID, agent_id)
    assert agent is not None
    assert agent.status == "ACTIVE"  # previous version remains LIVE
    assert agent.live_version == 1


async def test_passing_security_but_failing_ragas_still_blocks() -> None:
    registry_store, deployment_status_store = await _make_stores()
    agent_id = await _seed_agent_and_deployment(
        registry_store, deployment_status_store, "DEP-RAGAS-BLOCK", live_version=1
    )

    result = await enforce_policy_gate(
        [],  # no security findings at all
        tenant_id=TENANT_ID,
        agent_id=agent_id,
        deployment_id="DEP-RAGAS-BLOCK",
        updated_by="dev@example.com",
        deployment_status_store=deployment_status_store,
        registry_store=registry_store,
        evaluation_scores={"faithfulness": 0.10},
    )

    assert result.decision == "BLOCK"
    assert "faithfulness" in result.reason

    deployment = await deployment_status_store.get_deployment(agent_id, "DEP-RAGAS-BLOCK")
    assert deployment is not None
    assert deployment.status == "BLOCKED"
    assert deployment.failed_stage == "POLICY_CHECK"

    agent = await registry_store.get_agent(TENANT_ID, agent_id)
    assert agent is not None
    assert agent.status == "ACTIVE"  # previous version remains LIVE
    assert agent.live_version == 1


async def test_critical_security_finding_blocks_even_with_passing_ragas() -> None:
    registry_store, deployment_status_store = await _make_stores()
    agent_id = await _seed_agent_and_deployment(
        registry_store, deployment_status_store, "DEP-BOTH-1", live_version=None
    )

    result = await enforce_policy_gate(
        [
            SecurityFinding(
                scan_type="secret_scan",
                severity="CRITICAL",
                category="hardcoded_secret_found",
                description="AWS key committed",
            )
        ],
        tenant_id=TENANT_ID,
        agent_id=agent_id,
        deployment_id="DEP-BOTH-1",
        updated_by="dev@example.com",
        deployment_status_store=deployment_status_store,
        registry_store=registry_store,
        evaluation_scores={"faithfulness": 0.99},  # evaluation passes cleanly
    )

    assert result.decision == "BLOCK"
    assert "AWS key committed" in result.reason  # security reason, not RAGAS


async def test_passing_security_and_ragas_advances_deployment() -> None:
    registry_store, deployment_status_store = await _make_stores()
    agent_id = await _seed_agent_and_deployment(
        registry_store, deployment_status_store, "DEP-BOTH-PASS", live_version=1
    )

    result = await enforce_policy_gate(
        [],
        tenant_id=TENANT_ID,
        agent_id=agent_id,
        deployment_id="DEP-BOTH-PASS",
        updated_by="dev@example.com",
        deployment_status_store=deployment_status_store,
        registry_store=registry_store,
        evaluation_scores={"faithfulness": 0.99, "context_recall": 0.99},
    )

    assert result.decision == "PASS"

    deployment = await deployment_status_store.get_deployment(agent_id, "DEP-BOTH-PASS")
    assert deployment is not None
    assert deployment.status == "APPLYING"
    assert deployment.stages["POLICY_CHECK"].status == "PASSED"


async def test_no_evaluation_scores_means_evaluation_is_not_considered() -> None:
    # evaluation_scores omitted entirely (e.g. EVALUATING was SKIPPED, R14) —
    # behaves identically to Phase 9's security-only signature.
    registry_store, deployment_status_store = await _make_stores()
    agent_id = await _seed_agent_and_deployment(
        registry_store, deployment_status_store, "DEP-NO-EVAL", live_version=1
    )

    result = await enforce_policy_gate(
        [],
        tenant_id=TENANT_ID,
        agent_id=agent_id,
        deployment_id="DEP-NO-EVAL",
        updated_by="dev@example.com",
        deployment_status_store=deployment_status_store,
        registry_store=registry_store,
    )

    assert result.decision == "PASS"


async def test_passing_scan_advances_deployment_without_touching_agent() -> None:
    registry_store, deployment_status_store = await _make_stores()
    agent_id = await _seed_agent_and_deployment(
        registry_store, deployment_status_store, "DEP-PASS-1", live_version=1
    )

    result = await enforce_policy_gate(
        [
            SecurityFinding(
                scan_type="sast", severity="LOW", category="minor_style_issue", description="nit"
            )
        ],
        tenant_id=TENANT_ID,
        agent_id=agent_id,
        deployment_id="DEP-PASS-1",
        updated_by="dev@example.com",
        deployment_status_store=deployment_status_store,
        registry_store=registry_store,
    )

    assert result.decision == "PASS"

    deployment = await deployment_status_store.get_deployment(agent_id, "DEP-PASS-1")
    assert deployment is not None
    assert deployment.status == "APPLYING"
    assert deployment.stages["POLICY_CHECK"].status == "PASSED"

    agent = await registry_store.get_agent(TENANT_ID, agent_id)
    assert agent is not None
    assert agent.status == "ACTIVE"  # unchanged by enforce_policy_gate on PASS
