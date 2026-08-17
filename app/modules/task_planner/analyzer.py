"""Task Planner analysis logic (CLAUDE.md Section 38.6 Step 1 / 38.7 — A2-3).

Flow: build a snapshot of the tenant's existing catalog (tools, knowledge
bases, guardrail policies, skills) -> ask the factory-internal LLM to
propose a configuration using ONLY those resources -> parse the response
-> re-verify every suggested id against the real catalog before returning
anything to the caller. That last step is deliberate defense in depth: the
system prompt tells the LLM never to invent an id, but a prompt is not a
guarantee, so every "available" suggestion is cross-checked against the
actual catalog snapshot and demoted to "not_found"/id=None if it doesn't
match a real entry.
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
    ResourceSuggestion,
    TaskPlannerError,
    TaskPlannerProposal,
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
  "catalog_status": "not_found" and "id": null. Do NOT invent an id.
- Never invent a tool, knowledge base, guardrail policy, or skill name as if it
  were a real catalog entry. Every "available" suggestion's "id" must be copied
  verbatim from the catalog.
- "suggested_agent_type" must be exactly one of: conversational, task, rag,
  multi-step, orchestrator.
- "confidence" is a number between 0.0 and 1.0.
- Respond with ONLY a single valid JSON object matching this exact shape —
  no markdown code fences, no prose before or after the JSON. Each of
  "tools", "knowledge_bases", "guardrail_policies", "skills" is a list of
  objects shaped like RESOURCE_SUGGESTION:

RESOURCE_SUGGESTION = {"id": "string|null", "name": "string",
  "catalog_status": "available|not_found", "reason": "string"}

{
  "suggested_name": "string",
  "suggested_agent_type": "string",
  "suggested_persona": "string",
  "suggested_system_prompt": "string",
  "tools": [RESOURCE_SUGGESTION, ...],
  "knowledge_bases": [RESOURCE_SUGGESTION, ...],
  "guardrail_policies": [RESOURCE_SUGGESTION, ...],
  "skills": [RESOURCE_SUGGESTION, ...],
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

_CATEGORIES = ("tools", "knowledge_bases", "guardrail_policies", "skills")


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
    lines = ["EXISTING CATALOG (only these may be marked catalog_status=available):"]
    for category in _CATEGORIES:
        entries = catalog[category]
        lines.append(f"\n{category}:")
        if not entries:
            lines.append("  (none configured for this tenant)")
            continue
        for entry in entries:
            lines.append(
                f'  - id="{entry.id}" name="{entry.name}" description="{entry.description}"'
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


def _enforce_catalog_bound(
    suggestions: list[ResourceSuggestion], entries: list[_CatalogEntry]
) -> list[ResourceSuggestion]:
    valid_ids = {entry.id for entry in entries}
    enforced = []
    for s in suggestions:
        if s.id is not None and s.id in valid_ids:
            enforced.append(s)
        else:
            enforced.append(
                s.model_copy(update={"catalog_status": "not_found", "id": None})
            )
    return enforced


def _enforce_proposal_catalog_bound(
    proposal: TaskPlannerProposal, catalog: CatalogSnapshot
) -> TaskPlannerProposal:
    return proposal.model_copy(
        update={
            "tools": _enforce_catalog_bound(proposal.tools, catalog["tools"]),
            "knowledge_bases": _enforce_catalog_bound(
                proposal.knowledge_bases, catalog["knowledge_bases"]
            ),
            "guardrail_policies": _enforce_catalog_bound(
                proposal.guardrail_policies, catalog["guardrail_policies"]
            ),
            "skills": _enforce_catalog_bound(proposal.skills, catalog["skills"]),
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

    raw = await call_factory_model(model_id, _SYSTEM_PROMPT, user_message, max_tokens)
    proposal = _try_parse(raw)

    if proposal is None:
        raw = await call_factory_model(
            model_id, _SYSTEM_PROMPT, user_message + _RETRY_NUDGE, max_tokens
        )
        proposal = _try_parse(raw)

    if proposal is None:
        raise TaskPlannerError(
            "Task Planner did not return a valid proposal after retrying once."
        )

    return _enforce_proposal_catalog_bound(proposal, catalog)
