"""Build with AI approval executor (CLAUDE.md Section 42.4).

Turns an already-computed, stored `BuildWithAISessionRecord` into real
platform resources: creates the approved missing resources, then creates
every proposed agent (sub-agents before their orchestrator, so the
orchestrator's `orchestration.sub_agents` can reference real agent_ids),
wiring each agent's resources by matching the *name* the architecture
proposal used for that resource against either the tenant's existing
catalog (already-resolved `resource_id` on an in_catalog=true suggestion)
or a resource this same approval just created.

Never invents credentials (R48) — created connectors carry only
`credentials_required` (type labels), never a secret value.
"""

from __future__ import annotations

import re

from app.modules.connectors.catalog import ConnectorCatalogStore
from app.modules.guardrails.store import GuardrailPolicyStore
from app.modules.knowledge_base.store import KnowledgeBaseStore
from app.modules.registry.models import (
    AgentConfiguration,
    OrchestrationConfig,
    SkillConfig,
    SubAgentRef,
    ToolInstanceConfig,
)
from app.modules.registry.store import AgentRegistryStore
from app.modules.skills.store import SkillStore
from app.modules.task_planner.models import (
    AgentProposal,
    BuildWithAISessionRecord,
    CatalogSuggestion,
    CreatedAgentSummary,
    CreatedResourceSummary,
    ResourceKind,
)

_DEFAULT_MODEL_ID = "anthropic.claude-3-5-sonnet-20241022-v2:0"
_DEFAULT_MODEL_PROVIDER = "bedrock"


def _slugify_key(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "resource"


def _resource_key(kind: ResourceKind, name: str) -> str:
    return f"{kind}:{_slugify_key(name)}"


def _tool_instance(connector_id: str) -> ToolInstanceConfig:
    return ToolInstanceConfig(
        connector_id=connector_id,
        timeout_ms=10000,
        retry_count=1,
        cache_enabled=False,
        cache_ttl_seconds=300,
        error_handling="fail_request",
        fallback_connector_id=None,
        parallel_calls_allowed=True,
    )


async def _create_missing_resources(
    session: BuildWithAISessionRecord,
    skip_resource_keys: set[str],
    edited_configs: dict[str, dict[str, object]],
    tenant_id: str,
    created_by: str,
    connector_store: ConnectorCatalogStore,
    kb_store: KnowledgeBaseStore,
    guardrail_store: GuardrailPolicyStore,
    skill_store: SkillStore,
) -> tuple[dict[str, str], list[CreatedResourceSummary], list[str]]:
    """Returns (resource_key -> new resource_id, created summaries, skipped keys)."""
    created_lookup: dict[str, str] = {}
    created: list[CreatedResourceSummary] = []
    skipped: list[str] = []

    for missing in session.missing_resources:
        if missing.resource_key in skip_resource_keys:
            skipped.append(missing.resource_key)
            continue

        config = {**missing.proposed_config, **edited_configs.get(missing.resource_key, {})}

        if missing.resource_type == "tool":
            auth_type = config.get("auth_type", "none")
            connector = await connector_store.create_connector(
                tenant_id=tenant_id,
                name=missing.name,
                executor_type=config.get("executor_type", "http"),
                description=missing.description,
                created_by=created_by,
                input_schema=config.get("input_schema"),
                output_schema=config.get("output_schema"),
                endpoint_template=config.get("endpoint"),
                credentials_required=[auth_type] if auth_type and auth_type != "none" else [],
            )
            resource_id = connector.connector_id

        elif missing.resource_type == "knowledge_base":
            kb = await kb_store.create(
                tenant_id=tenant_id,
                name=missing.name,
                description=missing.description,
                source_type="manual",
                created_by=created_by,
            )
            resource_id = kb.kb_id

        elif missing.resource_type == "guardrail_policy":
            policy = await guardrail_store.create(
                tenant_id=tenant_id,
                name=missing.name,
                description=missing.description,
                created_by=created_by,
            )
            resource_id = policy.policy_id

        else:  # "skill"
            skill = await skill_store.create(
                tenant_id=tenant_id,
                name=missing.name,
                description=missing.description,
                capability=config.get("capability", missing.name),
                prompt_fragment=config.get(
                    "prompt_fragment", f"Use the {missing.name} skill when relevant."
                ),
                created_by=created_by,
            )
            resource_id = skill.skill_id

        created_lookup[missing.resource_key] = resource_id
        created.append(
            CreatedResourceSummary(
                resource_type=missing.resource_type, resource_id=resource_id, name=missing.name
            )
        )

    return created_lookup, created, skipped


def _resolve_id(
    suggestion: CatalogSuggestion, kind: ResourceKind, created_lookup: dict[str, str]
) -> str | None:
    if suggestion.in_catalog and suggestion.resource_id is not None:
        return suggestion.resource_id
    return created_lookup.get(_resource_key(kind, suggestion.name))


def _build_configuration(
    proposal: AgentProposal, created_lookup: dict[str, str]
) -> AgentConfiguration:
    tool_ids = [
        rid
        for t in proposal.tools
        if (rid := _resolve_id(t, "tool", created_lookup)) is not None
    ]
    kb_ids = [
        rid
        for k in proposal.knowledge_bases
        if (rid := _resolve_id(k, "knowledge_base", created_lookup)) is not None
    ]
    guardrail_id = (
        _resolve_id(proposal.guardrail_policy, "guardrail_policy", created_lookup)
        if proposal.guardrail_policy is not None
        else None
    )
    skill_ids = [
        rid
        for s in proposal.skills
        if (rid := _resolve_id(s, "skill", created_lookup)) is not None
    ]

    return AgentConfiguration(
        model_id=_DEFAULT_MODEL_ID,
        model_provider=_DEFAULT_MODEL_PROVIDER,
        system_prompt=proposal.system_prompt,
        kb_id=kb_ids[0] if kb_ids else None,
        guardrail_policy_id=guardrail_id,
        tool_instances=[_tool_instance(cid) for cid in tool_ids],
        skills=[SkillConfig(skill_id=sid, enabled=True) for sid in skill_ids],
    )


async def execute_approval(
    session: BuildWithAISessionRecord,
    skip_resource_keys: set[str],
    edited_configs: dict[str, dict[str, object]],
    tenant_id: str,
    created_by: str,
    connector_store: ConnectorCatalogStore,
    kb_store: KnowledgeBaseStore,
    guardrail_store: GuardrailPolicyStore,
    skill_store: SkillStore,
    registry_store: AgentRegistryStore,
) -> tuple[list[CreatedResourceSummary], list[str], list[CreatedAgentSummary]]:
    created_lookup, created_resources, skipped = await _create_missing_resources(
        session,
        skip_resource_keys,
        edited_configs,
        tenant_id,
        created_by,
        connector_store,
        kb_store,
        guardrail_store,
        skill_store,
    )

    created_agents: list[CreatedAgentSummary] = []
    sub_agent_refs: list[SubAgentRef] = []

    # Sub-agents first — the orchestrator (if any) needs their real agent_ids.
    for sub in session.architecture.sub_agents:
        configuration = _build_configuration(sub, created_lookup)
        record, _version = await registry_store.create_agent(
            tenant_id=tenant_id,
            name=sub.name,
            description=sub.description,
            business_purpose=sub.description,
            agent_type=sub.agent_type,
            configuration=configuration,
            created_by=created_by,
        )
        created_agents.append(
            CreatedAgentSummary(agent_id=record.agent_id, name=sub.name, role=sub.agent_type)
        )
        sub_agent_refs.append(
            SubAgentRef(
                agent_id=record.agent_id,
                agent_name=sub.name,
                capability_description=sub.capability_description,
            )
        )

    orchestrator = session.architecture.orchestrator
    orchestrator_configuration = _build_configuration(orchestrator, created_lookup)
    if sub_agent_refs:
        orchestrator_configuration = orchestrator_configuration.model_copy(
            update={
                "orchestration": OrchestrationConfig(
                    is_manager=True, sub_agents=sub_agent_refs
                )
            }
        )
    record, _version = await registry_store.create_agent(
        tenant_id=tenant_id,
        name=orchestrator.name,
        description=orchestrator.description,
        business_purpose=orchestrator.description,
        agent_type=orchestrator.agent_type,
        configuration=orchestrator_configuration,
        created_by=created_by,
    )
    created_agents.append(
        CreatedAgentSummary(
            agent_id=record.agent_id, name=orchestrator.name, role=orchestrator.agent_type
        )
    )

    return created_resources, skipped, created_agents
