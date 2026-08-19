"""Task Planner analysis logic (CLAUDE.md Section 38.6 Step 1 / 38.7 — A2-3).

Flow: build a snapshot of the tenant's existing catalog (tools, knowledge
bases, guardrail policies, skills) -> ask the factory-internal LLM to
propose a configuration using ONLY those resources -> parse the response
-> re-verify every suggested resource_id against the real catalog before
returning anything to the caller. That last step is deliberate defense in
depth: the system prompt tells the LLM never to invent an id, but a prompt
is not a guarantee, so every in_catalog=true suggestion is cross-checked
against the actual catalog snapshot and demoted to in_catalog=false/
resource_id=None if it doesn't match a real entry.
"""

from __future__ import annotations

import json
import re
from typing import Any

from pydantic import ValidationError

from app.modules.connectors.catalog import ConnectorCatalogStore
from app.modules.guardrails.store import GuardrailPolicyStore
from app.modules.knowledge_base.store import KnowledgeBaseStore
from app.modules.skills.store import SkillStore
from app.modules.task_planner.models import (
    AgentProposal,
    CatalogSuggestion,
    ResourceProposal,
    TaskPlannerError,
    TaskPlannerProposal,
    TaskPlannerResponse,
)
from app.services.model_router import call_factory_model

_VALID_AGENT_TYPES = ("conversational", "task", "rag", "multi-step", "orchestrator")

_SYSTEM_PROMPT = """You are an AI agent designer for an enterprise agent-building platform.

Given a plain-English description of what an agent should do, and a catalog of
resources that ALREADY EXIST on this platform, propose an agent configuration.

RULES — follow exactly, no exceptions:
- Only reference resources that appear in the EXISTING CATALOG section of the
  user message below.
- If a resource is needed but does not exist in the catalog, include it with
  "in_catalog": false and "resource_id": null. Do NOT invent an id.
- Never invent a tool, knowledge base, guardrail policy, or skill name as if it
  were a real catalog entry. Every "in_catalog": true suggestion's
  "resource_id" must be copied verbatim from the catalog.
- "suggested_agent_type" must be exactly one of: conversational, task, rag,
  multi-step, orchestrator.
- "suggested_guardrail_policy" is a SINGLE object (the one most relevant
  policy) or null — never a list.
- "confidence" is a number between 0.0 and 1.0.
- Respond with ONLY a single valid JSON object matching this exact shape —
  no markdown code fences, no prose before or after the JSON. Each of
  "suggested_tools", "suggested_knowledge_bases", "suggested_skills" is a
  list of objects shaped like RESOURCE_SUGGESTION; "suggested_guardrail_policy"
  is one RESOURCE_SUGGESTION or null:

RESOURCE_SUGGESTION = {"name": "string", "description": "string|null",
  "in_catalog": true|false, "resource_id": "string|null"}

{
  "suggested_name": "string",
  "suggested_description": "string",
  "suggested_agent_type": "string",
  "suggested_persona_name": "string|null",
  "suggested_system_prompt": "string",
  "suggested_tools": [RESOURCE_SUGGESTION, ...],
  "suggested_knowledge_bases": [RESOURCE_SUGGESTION, ...],
  "suggested_guardrail_policy": RESOURCE_SUGGESTION or null,
  "suggested_skills": [RESOURCE_SUGGESTION, ...],
  "suggested_output_format": "string|null",
  "confidence": 0.0,
  "reasoning": "string"
}
"""

_RETRY_NUDGE = (
    "\n\nYour previous response could not be parsed as valid JSON matching the "
    "required schema. Respond again with ONLY the JSON object — no markdown "
    "code fences, no explanation before or after it."
)


class _CatalogEntry:
    __slots__ = ("id", "name", "description")

    def __init__(self, id_: str, name: str, description: str) -> None:
        self.id = id_
        self.name = name
        self.description = description


CatalogSnapshot = dict[str, list[_CatalogEntry]]

_LIST_CATEGORIES = ("tools", "knowledge_bases", "skills")
_CATEGORIES = (*_LIST_CATEGORIES, "guardrail_policies")


async def _build_catalog(
    tenant_id: str,
    connector_store: ConnectorCatalogStore,
    kb_store: KnowledgeBaseStore,
    guardrail_store: GuardrailPolicyStore,
    skill_store: SkillStore,
) -> CatalogSnapshot:
    connectors = await connector_store.list_connectors(tenant_id)
    kbs = await kb_store.list_knowledge_bases(tenant_id)
    policies = await guardrail_store.list_policies(tenant_id)
    skills = await skill_store.list_skills(tenant_id)

    return {
        "tools": [_CatalogEntry(c.connector_id, c.name, c.description) for c in connectors],
        "knowledge_bases": [_CatalogEntry(k.kb_id, k.name, k.description) for k in kbs],
        "guardrail_policies": [_CatalogEntry(p.policy_id, p.name, p.description) for p in policies],
        "skills": [_CatalogEntry(s.skill_id, s.name, s.description) for s in skills],
    }


def _render_catalog(catalog: CatalogSnapshot) -> str:
    lines = ["EXISTING CATALOG (only these may be marked in_catalog=true):"]
    for category in _CATEGORIES:
        entries = catalog[category]
        lines.append(f"\n{category}:")
        if not entries:
            lines.append("  (none configured for this tenant)")
            continue
        for entry in entries:
            lines.append(
                f'  - resource_id="{entry.id}" name="{entry.name}" '
                f'description="{entry.description}"'
            )
    return "\n".join(lines)


def _build_user_message(description: str, catalog: CatalogSnapshot) -> str:
    return (
        f"Agent description:\n{description}\n\n"
        f"{_render_catalog(catalog)}\n\n"
        "Propose an agent configuration using only the rules and JSON shape "
        "from the system prompt."
    )


def _strip_code_fence(text: str) -> str:
    stripped = text.strip()
    match = re.match(r"^```(?:json)?\s*(.*?)\s*```$", stripped, re.DOTALL)
    return match.group(1) if match else stripped


def _try_parse(raw: str) -> TaskPlannerProposal | None:
    try:
        data: Any = json.loads(_strip_code_fence(raw))
    except json.JSONDecodeError:
        return None

    if not isinstance(data, dict):
        return None

    if data.get("suggested_agent_type") not in _VALID_AGENT_TYPES:
        return None

    try:
        return TaskPlannerProposal(**data)
    except ValidationError:
        return None


def _enforce_list(
    suggestions: list[CatalogSuggestion], entries: list[_CatalogEntry]
) -> list[CatalogSuggestion]:
    valid_ids = {entry.id for entry in entries}
    enforced = []
    for s in suggestions:
        if s.in_catalog and s.resource_id is not None and s.resource_id in valid_ids:
            enforced.append(s)
        else:
            enforced.append(s.model_copy(update={"in_catalog": False, "resource_id": None}))
    return enforced


def _enforce_single(
    suggestion: CatalogSuggestion | None, entries: list[_CatalogEntry]
) -> CatalogSuggestion | None:
    if suggestion is None:
        return None
    valid_ids = {entry.id for entry in entries}
    is_valid = (
        suggestion.in_catalog
        and suggestion.resource_id is not None
        and suggestion.resource_id in valid_ids
    )
    if is_valid:
        return suggestion
    return suggestion.model_copy(update={"in_catalog": False, "resource_id": None})


def _enforce_proposal_catalog_bound(
    proposal: TaskPlannerProposal, catalog: CatalogSnapshot
) -> TaskPlannerProposal:
    return proposal.model_copy(
        update={
            "suggested_tools": _enforce_list(proposal.suggested_tools, catalog["tools"]),
            "suggested_knowledge_bases": _enforce_list(
                proposal.suggested_knowledge_bases, catalog["knowledge_bases"]
            ),
            "suggested_guardrail_policy": _enforce_single(
                proposal.suggested_guardrail_policy, catalog["guardrail_policies"]
            ),
            "suggested_skills": _enforce_list(proposal.suggested_skills, catalog["skills"]),
        }
    )


async def analyze(
    description: str,
    tenant_id: str,
    connector_store: ConnectorCatalogStore,
    kb_store: KnowledgeBaseStore,
    guardrail_store: GuardrailPolicyStore,
    skill_store: SkillStore,
    model_id: str,
    max_tokens: int,
) -> TaskPlannerProposal:
    catalog = await _build_catalog(
        tenant_id, connector_store, kb_store, guardrail_store, skill_store
    )
    user_message = _build_user_message(description, catalog)

    try:
        raw = await call_factory_model(model_id, _SYSTEM_PROMPT, user_message, max_tokens)
    except Exception as exc:
        # call_factory_model has no error handling of its own — an LLM-side
        # failure (auth, throttling, timeout, model not found) must never
        # surface as a bare 500 with no detail. TaskPlannerError is already
        # the one exception type task_planner.py's route maps to a 502, so
        # every failure mode funnels through the same clean response shape.
        raise TaskPlannerError(f"Task Planner's model call failed: {exc}") from exc
    proposal = _try_parse(raw)

    if proposal is None:
        try:
            raw = await call_factory_model(
                model_id, _SYSTEM_PROMPT, user_message + _RETRY_NUDGE, max_tokens
            )
        except Exception as exc:
            raise TaskPlannerError(f"Task Planner's model call failed: {exc}") from exc
        proposal = _try_parse(raw)

    if proposal is None:
        raise TaskPlannerError(
            "Task Planner did not return a valid proposal after retrying once."
        )

    return _enforce_proposal_catalog_bound(proposal, catalog)


# ── Multi-agent architecture proposal (Wizard Redesign, 2026-08-18) ─────────
# See the module-level comment in task_planner/models.py: additive, parallel
# to analyze()/TaskPlannerProposal above, not a replacement.

_VALID_ARCHITECTURE_AGENT_TYPES = ("standard", "orchestrator")

_AGENT_PROPOSAL_SHAPE = """AGENT_PROPOSAL = {
  "name": "string", "description": "string",
  "agent_type": "standard|orchestrator",
  "persona_name": "string|null", "system_prompt": "string",
  "capability_description": "string",
  "tools": [RESOURCE_SUGGESTION, ...], "knowledge_bases": [RESOURCE_SUGGESTION, ...],
  "guardrail_policy": RESOURCE_SUGGESTION or null, "skills": [RESOURCE_SUGGESTION, ...]
}"""

_ARCHITECTURE_SYSTEM_PROMPT = f"""You are an AI agent architect for an enterprise \
agent-building platform.

Given a plain-English description of what needs to be accomplished, and a catalog of
resources that ALREADY EXIST on this platform, propose an agent architecture. Decide
for yourself whether this needs one agent or several working together:
- If the requirement is simple and one agent can handle it end to end, propose a
  single agent as "orchestrator" with agent_type "standard" and an empty
  "sub_agents" list. Do NOT invent extra sub-agents just to seem thorough.
- If the requirement genuinely needs multiple specialised agents coordinated by a
  manager, propose "orchestrator" with agent_type "orchestrator" plus one
  AgentProposal per specialist in "sub_agents". Give every sub-agent a clear,
  specific "capability_description" the orchestrator's router can use to decide
  which sub-agent handles a given request.

RULES — follow exactly, no exceptions:
- Only reference resources that appear in the EXISTING CATALOG section of the
  user message below.
- If a resource is needed but does not exist in the catalog, include it with
  "in_catalog": false and "resource_id": null. Do NOT invent an id.
- Never invent a tool, knowledge base, guardrail policy, or skill name as if it
  were a real catalog entry. Every "in_catalog": true suggestion's "resource_id"
  must be copied verbatim from the catalog.
- "agent_type" must be exactly one of: standard, orchestrator. This is the
  agent's structural ROLE only — "standard" is a single node (whether or not
  it has a knowledge base or tools attached), "orchestrator" manages
  sub-agents. Retrieval/tool-use are capabilities, not roles.
- "guardrail_policy" on each agent is a SINGLE object (the one most relevant
  policy) or null — never a list.
- "confidence" is a number between 0.0 and 1.0.
- Respond with ONLY a single valid JSON object matching this exact shape — no
  markdown code fences, no prose before or after the JSON:

RESOURCE_SUGGESTION = {{"name": "string", "description": "string|null",
  "in_catalog": true|false, "resource_id": "string|null"}}

{_AGENT_PROPOSAL_SHAPE}

{{
  "orchestrator": AGENT_PROPOSAL,
  "sub_agents": [AGENT_PROPOSAL, ...],
  "output_schema": "string|null",
  "confidence": 0.0,
  "reasoning": "string"
}}
"""


_ArchitectureParseResult = tuple[AgentProposal, list[AgentProposal], str | None, float, str]


def _try_parse_architecture(raw: str) -> _ArchitectureParseResult | None:
    try:
        data: Any = json.loads(_strip_code_fence(raw))
    except json.JSONDecodeError:
        return None

    if not isinstance(data, dict):
        return None

    orchestrator_data = data.get("orchestrator")
    if not isinstance(orchestrator_data, dict):
        return None
    if orchestrator_data.get("agent_type") not in _VALID_ARCHITECTURE_AGENT_TYPES:
        return None

    sub_agents_data = data.get("sub_agents", [])
    if not isinstance(sub_agents_data, list):
        return None
    for sub in sub_agents_data:
        if not isinstance(sub, dict):
            return None
        if sub.get("agent_type") not in _VALID_ARCHITECTURE_AGENT_TYPES:
            return None

    try:
        orchestrator = AgentProposal(**orchestrator_data)
        sub_agents = [AgentProposal(**sub) for sub in sub_agents_data]
    except ValidationError:
        return None

    return (
        orchestrator,
        sub_agents,
        data.get("output_schema"),
        data.get("confidence", 0.5),
        data.get("reasoning", ""),
    )


def _enforce_agent_proposal_catalog_bound(
    proposal: AgentProposal, catalog: CatalogSnapshot
) -> AgentProposal:
    return proposal.model_copy(
        update={
            "tools": _enforce_list(proposal.tools, catalog["tools"]),
            "knowledge_bases": _enforce_list(proposal.knowledge_bases, catalog["knowledge_bases"]),
            "guardrail_policy": _enforce_single(
                proposal.guardrail_policy, catalog["guardrail_policies"]
            ),
            "skills": _enforce_list(proposal.skills, catalog["skills"]),
        }
    )


def _dedupe_suggestions(suggestions: list[CatalogSuggestion]) -> list[CatalogSuggestion]:
    seen: set[str] = set()
    deduped: list[CatalogSuggestion] = []
    for s in suggestions:
        key = s.resource_id if (s.in_catalog and s.resource_id is not None) else f"name:{s.name}"
        if key in seen:
            continue
        seen.add(key)
        deduped.append(s)
    return deduped


def _build_resource_proposal(agents: list[AgentProposal]) -> ResourceProposal:
    guardrails = [a.guardrail_policy for a in agents if a.guardrail_policy is not None]
    return ResourceProposal(
        tools=_dedupe_suggestions([t for a in agents for t in a.tools]),
        knowledge_bases=_dedupe_suggestions([k for a in agents for k in a.knowledge_bases]),
        guardrail_policies=_dedupe_suggestions(guardrails),
        skills=_dedupe_suggestions([s for a in agents for s in a.skills]),
    )


async def analyze_architecture(
    description: str,
    tenant_id: str,
    connector_store: ConnectorCatalogStore,
    kb_store: KnowledgeBaseStore,
    guardrail_store: GuardrailPolicyStore,
    skill_store: SkillStore,
    model_id: str,
    max_tokens: int,
) -> TaskPlannerResponse:
    catalog = await _build_catalog(
        tenant_id, connector_store, kb_store, guardrail_store, skill_store
    )
    user_message = _build_user_message(description, catalog)

    try:
        raw = await call_factory_model(
            model_id, _ARCHITECTURE_SYSTEM_PROMPT, user_message, max_tokens
        )
    except Exception as exc:
        raise TaskPlannerError(f"Task Planner's model call failed: {exc}") from exc
    parsed = _try_parse_architecture(raw)

    if parsed is None:
        try:
            raw = await call_factory_model(
                model_id,
                _ARCHITECTURE_SYSTEM_PROMPT,
                user_message + _RETRY_NUDGE,
                max_tokens,
            )
        except Exception as exc:
            raise TaskPlannerError(f"Task Planner's model call failed: {exc}") from exc
        parsed = _try_parse_architecture(raw)

    if parsed is None:
        raise TaskPlannerError(
            "Task Planner did not return a valid architecture proposal after retrying once."
        )

    orchestrator, sub_agents, output_schema, confidence, reasoning = parsed
    orchestrator = _enforce_agent_proposal_catalog_bound(orchestrator, catalog)
    sub_agents = [_enforce_agent_proposal_catalog_bound(sub, catalog) for sub in sub_agents]

    return TaskPlannerResponse(
        orchestrator=orchestrator,
        sub_agents=sub_agents,
        resources=_build_resource_proposal([orchestrator, *sub_agents]),
        output_schema=output_schema,
        confidence=confidence,
        reasoning=reasoning,
    )
