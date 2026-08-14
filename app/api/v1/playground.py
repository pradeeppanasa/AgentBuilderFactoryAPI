"""Agent playground (CLAUDE_Advanced_Config.md Section 3.9 / 37.9).

Built-in test environment on the agent detail page — never counts as a
production invocation (a separate `panasa-playground-sessions` table, not
`panasa-transcripts`), but guardrail decisions are still audited (R14/R30).

Scope boundary (F8): tool execution and KB retrieval are capabilities of
the *Generated Agent Runtime*, a separate service this Builder Runtime does
not build. `tool_calls` and `kb_retrievals` are honestly returned
empty/stubbed here rather than faked — see the docstrings on
`_run_tool_calls`/`_run_kb_retrieval` below.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from app.dependencies import (
    get_audit_writer,
    get_guardrail_engine,
    get_guardrail_policy_store,
    get_playground_session_store,
    get_registry_store,
    get_tenant_id,
)
from app.modules.audit.writer import AuditEvent, AuditWriter
from app.modules.auth.dependencies import require_role
from app.modules.auth.schemas import CurrentUser
from app.modules.guardrails.engine import GuardrailEngine
from app.modules.guardrails.models import GuardrailDecision
from app.modules.guardrails.store import GuardrailPolicyStore
from app.modules.playground.models import PlaygroundTurn
from app.modules.playground.store import PlaygroundSessionStore
from app.modules.registry.store import AgentRegistryStore
from app.services.model_router import call_model

router = APIRouter(prefix="/agents", tags=["playground"])

_READ_ROLES = ("developer", "analyst", "auditor")

_BLOCKED_REPLY = "This message was blocked by a guardrail policy."


class PlaygroundOverrides(BaseModel):
    """Admin-only (Section 37.9). Any non-default value here from a
    non-admin caller is rejected with 403 — see `_reject_overrides_if_not_admin`."""

    disable_guardrails: bool = False
    temperature: float | None = None
    clear_memory: bool = False


class PlaygroundRequest(BaseModel):
    message: str
    session_id: str | None = None
    overrides: PlaygroundOverrides = Field(default_factory=PlaygroundOverrides)


class GuardrailDecisionSummary(BaseModel):
    layer: str
    action: str
    confidence: float | None = None


class ToolCallSummary(BaseModel):
    name: str
    duration_ms: int
    success: bool
    cached: bool


class KBRetrievalSummary(BaseModel):
    chunk_count: int
    similarity_scores: list[float]


class MemoryStateSummary(BaseModel):
    session_entries: int
    long_term_entries_used: int


class LatencyBreakdown(BaseModel):
    guardrail_ms: int
    retrieval_ms: int
    llm_ms: int
    total_ms: int


class TokenUsageSummary(BaseModel):
    input_tokens: int | None = None
    output_tokens: int | None = None
    kb_context_tokens: int | None = None
    memory_context_tokens: int | None = None


class PlaygroundMetrics(BaseModel):
    latency: LatencyBreakdown
    tokens: TokenUsageSummary
    estimated_cost_usd: float | None
    guardrail_decisions: list[GuardrailDecisionSummary]
    tool_calls: list[ToolCallSummary]
    kb_retrievals: KBRetrievalSummary | None
    memory: MemoryStateSummary


class PlaygroundResponse(BaseModel):
    session_id: str
    blocked: bool
    message: str
    metrics: PlaygroundMetrics


def _reject_overrides_if_not_admin(
    overrides: PlaygroundOverrides, current_user: CurrentUser
) -> None:
    if current_user.role == "admin":
        return
    if overrides != PlaygroundOverrides():
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                "Playground overrides (disable_guardrails, temperature, "
                "clear_memory) are admin-only"
            ),
        )


def _run_tool_calls() -> list[ToolCallSummary]:
    """Tool execution belongs to the Generated Agent Runtime (F8), a
    separate service this Builder Runtime does not build. Always empty
    here — never fabricated — until that boundary changes."""
    return []


def _run_kb_retrieval(kb_id: str | None) -> KBRetrievalSummary | None:
    """Real KB retrieval (embedding query against OpenSearch) is Generated
    Agent Runtime infrastructure (F8) this Builder Runtime does not build.
    Reports an honest zero-chunk stub when a KB is configured, rather than
    fabricating retrieval results, so the metrics panel doesn't imply a
    capability that doesn't exist yet."""
    if kb_id is None:
        return None
    return KBRetrievalSummary(chunk_count=0, similarity_scores=[])


@router.post("/{agent_id}/playground", response_model=PlaygroundResponse)
async def run_playground_turn(
    agent_id: str,
    payload: PlaygroundRequest,
    tenant_id: Annotated[str, Depends(get_tenant_id)],
    current_user: Annotated[CurrentUser, Depends(require_role(*_READ_ROLES))],
    registry_store: Annotated[AgentRegistryStore, Depends(get_registry_store)],
    guardrail_policy_store: Annotated[GuardrailPolicyStore, Depends(get_guardrail_policy_store)],
    guardrail_engine: Annotated[GuardrailEngine, Depends(get_guardrail_engine)],
    session_store: Annotated[PlaygroundSessionStore, Depends(get_playground_session_store)],
    audit_writer: Annotated[AuditWriter, Depends(get_audit_writer)],
) -> PlaygroundResponse:
    _reject_overrides_if_not_admin(payload.overrides, current_user)

    agent = await registry_store.get_agent(tenant_id, agent_id)
    if agent is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"Agent {agent_id!r} not found"
        )
    version = await registry_store.get_version(agent_id, agent.current_version)
    if version is None:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Current version {agent.current_version} missing for agent {agent_id!r}",
        )
    config = version.configuration

    session_id = None if payload.overrides.clear_memory else payload.session_id
    session = await session_store.get_or_create(tenant_id, agent_id, session_id)

    policy = None
    if config.guardrail_policy_id and not payload.overrides.disable_guardrails:
        policy = await guardrail_policy_store.get(tenant_id, config.guardrail_policy_id)

    guardrail_decisions: list[GuardrailDecisionSummary] = []
    guardrail_ms = 0.0

    def _record_layers(decision: GuardrailDecision) -> None:
        guardrail_decisions.extend(
            GuardrailDecisionSummary(
                layer=layer.layer, action=layer.action, confidence=layer.confidence
            )
            for layer in decision.layers
        )

    blocked = False
    reply_text = ""
    cost: float | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    llm_ms = 0.0

    if policy is not None:
        start = time.perf_counter()
        input_decision = await guardrail_engine.check_input(payload.message, policy)
        guardrail_ms += (time.perf_counter() - start) * 1000
        _record_layers(input_decision)
        blocked = input_decision.blocked

    if not blocked:
        effective_config = config
        if payload.overrides.temperature is not None:
            effective_config = config.model_copy(
                update={"temperature": payload.overrides.temperature}
            )

        start = time.perf_counter()
        reply_text, cost, usage = await call_model(
            effective_config,
            [
                {"role": "system", "content": config.system_prompt},
                {"role": "user", "content": payload.message},
            ],
        )
        llm_ms = (time.perf_counter() - start) * 1000
        if usage is not None:
            input_tokens, output_tokens = usage.input_tokens, usage.output_tokens

        if policy is not None:
            start = time.perf_counter()
            output_decision = await guardrail_engine.check_output(reply_text, policy)
            guardrail_ms += (time.perf_counter() - start) * 1000
            _record_layers(output_decision)
            if output_decision.blocked:
                blocked = True
            elif output_decision.sanitised_text is not None:
                reply_text = output_decision.sanitised_text

    if blocked:
        reply_text = _BLOCKED_REPLY

    retrieval_start = time.perf_counter()
    kb_retrievals = _run_kb_retrieval(config.kb_id)
    retrieval_ms = (time.perf_counter() - retrieval_start) * 1000

    now = datetime.now(UTC).isoformat()
    turns = [
        PlaygroundTurn(
            turn_id=len(session.turns), role="user", content=payload.message, created_at=now
        ),
        PlaygroundTurn(
            turn_id=len(session.turns) + 1, role="assistant", content=reply_text, created_at=now
        ),
    ]
    updated_session = await session_store.append_turns(agent_id, session.session_id, turns)

    if guardrail_decisions:
        await audit_writer.write(
            AuditEvent(
                event_type="guardrail_decision",
                tenant_id=tenant_id,
                agent_id=agent_id,
                actor=current_user.email,
                summary=(
                    f"playground: {len(guardrail_decisions)} guardrail layer(s) evaluated, "
                    f"blocked={blocked}"
                ),
                metadata={
                    "session_id": updated_session.session_id,
                    "layers": [d.model_dump() for d in guardrail_decisions],
                },
                occurred_at=now,
            )
        )

    total_ms = guardrail_ms + retrieval_ms + llm_ms

    return PlaygroundResponse(
        session_id=updated_session.session_id,
        blocked=blocked,
        message=reply_text,
        metrics=PlaygroundMetrics(
            latency=LatencyBreakdown(
                guardrail_ms=round(guardrail_ms),
                retrieval_ms=round(retrieval_ms),
                llm_ms=round(llm_ms),
                total_ms=round(total_ms),
            ),
            tokens=TokenUsageSummary(input_tokens=input_tokens, output_tokens=output_tokens),
            estimated_cost_usd=cost,
            guardrail_decisions=guardrail_decisions,
            tool_calls=_run_tool_calls(),
            kb_retrievals=kb_retrievals,
            memory=MemoryStateSummary(
                session_entries=len(updated_session.turns), long_term_entries_used=0
            ),
        ),
    )
