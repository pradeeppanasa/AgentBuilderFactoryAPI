"""Per-request orchestration — one LLM turn, with guardrails, memory, RAG,
tool use, and HITL wired around it.

`config` is the plain dict config_loader.py returns (this agent's own
AgentConfiguration, flattened with agent_id/tenant_id/name/version) —
every field access below uses .get() with a sensible default rather than
assuming a key exists, since this service deliberately doesn't share a
Pydantic model with the Factory Runtime (F8; see config_loader.py's
docstring) and a config the Factory Runtime considers valid today may add
fields tomorrow that an already-running (not yet redeployed) task has
never heard of.
"""

from __future__ import annotations

import json
import time
import uuid
from typing import Any

import structlog
from audit import write_audit_event
from guardrail import GuardrailChecker, blocked_response_text
from hitl import HITLManager
from llm_client import LLMClient
from memory import MemoryManager
from prompt_builder import PromptBuilder, PromptContext
from quota import record_llm_cost
from rag_client import RAGClient
from tool_approval import ToolApprovalManager
from tool_executor import ToolExecutor
from tool_policy import ApprovalRequiredError

logger = structlog.get_logger()

_HITL_PENDING_MESSAGE = "This request requires human review. You will be notified."
_APPROVAL_PENDING_MESSAGE = (
    "This action requires human approval before it can proceed. "
    "You will be notified once a decision is made."
)
_APPROVAL_STILL_PENDING_MESSAGE = "This action is still awaiting human approval."
_APPROVAL_ALREADY_PROCESSED_MESSAGE = "This action has already been processed."


class AgentOrchestrator:
    def __init__(self, config: dict[str, Any]) -> None:
        self.config = config
        self.agent_id: str = config["agent_id"]
        self.tenant_id: str = config["tenant_id"]

        self.llm = LLMClient(
            model_id=config["model_id"],
            model_provider=config["model_provider"],
            system_prompt=config["system_prompt"],
            temperature=config.get("temperature", 0.3),
            max_tokens=config.get("max_tokens", 2048),
            fallback_model_string=config.get("fallback_model_string"),
        )
        self.rag = RAGClient(tenant_id=self.tenant_id, kb_config=config.get("knowledge_base"))
        self.tools = ToolExecutor(
            agent_id=self.agent_id,
            tenant_id=self.tenant_id,
            tools=config.get("tools") or [],
            tool_policies=config.get("tool_policies") or [],
        )

        memory_config = config.get("memory") or {}
        self.memory = MemoryManager(
            agent_id=self.agent_id,
            memory_type=memory_config.get("memory_type", "none"),
            ttl_days=memory_config.get("persistent_memory_ttl_days", 30),
            max_session_turns=memory_config.get("max_session_turns", 50),
        )

        self.guardrail = GuardrailChecker(
            tenant_id=self.tenant_id, policy_id=config.get("guardrail_policy_id")
        )

        human_review = config.get("human_review") or {}
        self.hitl = HITLManager(
            agent_id=self.agent_id,
            tenant_id=self.tenant_id,
            enabled=bool(human_review.get("enabled", False)),
            trigger_conditions=human_review.get("trigger_conditions") or [],
            timeout_hours=human_review.get("approval_timeout_hours", 24),
            notification_sns_arn=human_review.get("notification_sns_arn"),
        )

        # Sprint 4 Phase 3 (S-11, R64) — HIGH/DESTRUCTIVE tool-call
        # approvals reuse `human_review`'s per-agent notification_sns_arn/
        # approval_timeout_hours rather than introducing a second config
        # block, since no other per-agent SNS ARN exists anywhere in this
        # config shape today (see tool_approval.py's module docstring).
        self.tool_approval = ToolApprovalManager(
            agent_id=self.agent_id,
            tenant_id=self.tenant_id,
            notification_sns_arn=human_review.get("notification_sns_arn"),
            timeout_hours=human_review.get("approval_timeout_hours", 24),
        )

        self.prompt_builder = PromptBuilder()

    async def run(
        self, message: str, session_id: str, user_id: str | None = None
    ) -> dict[str, Any]:
        run_id = str(uuid.uuid4())
        # Sprint 4 Phase 2 (S-05) — agent.invoked's own latency_ms/token
        # fields (CLAUDE.md Section 61.2). Started before any early-return
        # branch so every path (guardrail-blocked, HITL-paused, completed)
        # reports a real latency, not just the ones that reach the LLM.
        started = time.perf_counter()
        total_input_tokens = 0
        total_output_tokens = 0
        logger.info("run_started", run_id=run_id, agent_id=self.agent_id)

        input_check = await self.guardrail.check(message, source="INPUT")
        if input_check.blocked:
            logger.warning("run_blocked_by_guardrail", run_id=run_id, reason=input_check.reason)
            # Sprint 4 Phase 2 (S-05, CLAUDE.md Section 61.2) — reason is a
            # short category tag (GuardrailResult's own contract), never
            # the actual message content (R30/R61).
            write_audit_event(
                tenant_id=self.tenant_id,
                event_type="guardrail.input_blocked",
                agent_id=self.agent_id,
                principal_id="agent-runtime",
                action="guardrail_check:INPUT",
                resource=self.agent_id,
                result="denied",
                extra={"reason": input_check.reason},
            )
            return {
                "response": blocked_response_text(),
                "session_id": session_id,
                "run_id": run_id,
                "hitl_pending": False,
                "latency_ms": int((time.perf_counter() - started) * 1000),
                "input_tokens": total_input_tokens,
                "output_tokens": total_output_tokens,
            }
        effective_message = input_check.sanitised_text or message

        memory_context = await self.memory.load(session_id=session_id, user_id=user_id)

        rag_context = await self.rag.retrieve(query=effective_message)

        # R65/PromptBuilder — kb_content and session_history (both
        # untrusted) are placed in their own clearly-labelled messages,
        # never silently concatenated into the user's own turn; the
        # system_prompt this returns is informational only (see
        # prompt_builder.py's docstring) — llm.complete() below never
        # receives it, since LLMClient already owns the TRUSTED system
        # prompt immutably from construction.
        _system_unused, initial_messages = self.prompt_builder.build_messages(
            PromptContext(
                system_prompt=self.config["system_prompt"],
                user_message=effective_message,
                kb_content=rag_context,
                session_history=memory_context,
            )
        )

        hitl_result = await self.hitl.pre_check(
            message=effective_message, context=str(initial_messages)
        )
        if hitl_result.get("pause"):
            await self.hitl.create_review(
                run_id=run_id,
                agent_id=self.agent_id,
                message=effective_message,
                session_id=session_id,
                trigger_condition=hitl_result.get("trigger_condition", "unspecified"),
            )
            return {
                "response": _HITL_PENDING_MESSAGE,
                "session_id": session_id,
                "run_id": run_id,
                "hitl_pending": True,
                "latency_ms": int((time.perf_counter() - started) * 1000),
                "input_tokens": total_input_tokens,
                "output_tokens": total_output_tokens,
            }

        llm_response = await self.llm.complete(
            messages=initial_messages,
            tools=self.tools.get_definitions(),
        )
        # Sprint 3 Phase 9 (S-03, Section 67.3) — record_llm_cost() is an
        # atomic DynamoDB ADD, so recording each llm.complete() call's cost
        # the moment it happens (rather than accumulating a total to record
        # once at the end) is exactly equivalent for the normal path, AND
        # correctly captures this call's real, already-incurred cost even
        # when a HIGH/DESTRUCTIVE tool call below pauses the turn for human
        # approval and returns early (Sprint 4 Phase 3, S-11) — a total
        # recorded "at the end" would have silently lost that cost.
        record_llm_cost(self.tenant_id, self.agent_id, llm_response.cost_usd or 0.0)
        total_input_tokens += llm_response.input_tokens or 0
        total_output_tokens += llm_response.output_tokens or 0

        if llm_response.tool_calls:
            try:
                tool_results = await self.tools.execute(llm_response.tool_calls)
            except ApprovalRequiredError as exc:
                return await self._request_tool_approval(
                    exc,
                    llm_response=llm_response,
                    initial_messages=initial_messages,
                    effective_message=effective_message,
                    session_id=session_id,
                    user_id=user_id,
                    run_id=run_id,
                    started=started,
                    total_input_tokens=total_input_tokens,
                    total_output_tokens=total_output_tokens,
                )
            final = await self.llm.complete(
                messages=[
                    *initial_messages,
                    {
                        "role": "assistant",
                        "content": llm_response.content,
                        "tool_calls": llm_response.tool_calls,
                    },
                    {"role": "tool", "content": str(tool_results)},
                ]
            )
            record_llm_cost(self.tenant_id, self.agent_id, final.cost_usd or 0.0)
            total_input_tokens += final.input_tokens or 0
            total_output_tokens += final.output_tokens or 0
            final_response = final.content
        else:
            final_response = llm_response.content

        return await self._finalize(
            final_response,
            session_id=session_id,
            user_id=user_id,
            effective_message=effective_message,
            run_id=run_id,
            started=started,
            total_input_tokens=total_input_tokens,
            total_output_tokens=total_output_tokens,
        )

    async def _request_tool_approval(
        self,
        exc: ApprovalRequiredError,
        *,
        llm_response: Any,
        initial_messages: list[dict[str, Any]],
        effective_message: str,
        session_id: str,
        user_id: str | None,
        run_id: str,
        started: float,
        total_input_tokens: int,
        total_output_tokens: int,
    ) -> dict[str, Any]:
        """Sprint 4 Phase 3 (S-11) — persists everything resume_after_approval()
        needs to re-invoke the tool and continue this same turn once a human
        decides, then returns the "awaiting approval" response. Matching call
        found by tool name (`exc.tool_id`), not LLM call id — see
        tool_executor.py's module docstring for the known multi-call limitation
        this implies."""
        matching_call = next(
            (c for c in llm_response.tool_calls if c.get("name") == exc.tool_id), None
        )
        resume_context = json.dumps(
            {
                "session_id": session_id,
                "user_id": user_id,
                "effective_message": effective_message,
                "initial_messages": initial_messages,
                "assistant_content": llm_response.content,
                "tool_calls": llm_response.tool_calls,
                "approved_call_id": matching_call.get("id") if matching_call else None,
                "tool_id": exc.tool_id,
            }
        )
        review_id = await self.tool_approval.request_approval(
            tool_id=exc.tool_id,
            run_id=run_id,
            session_id=session_id,
            resume_context=resume_context,
        )
        return {
            "response": _APPROVAL_PENDING_MESSAGE,
            "session_id": session_id,
            "run_id": run_id,
            "hitl_pending": True,
            "approval_review_id": review_id,
            "approval_status": "pending",
            "latency_ms": int((time.perf_counter() - started) * 1000),
            "input_tokens": total_input_tokens,
            "output_tokens": total_output_tokens,
        }

    async def resume_after_approval(self, review_id: str) -> dict[str, Any]:
        """Called by main.py's POST /approvals/{review_id}/resume once a
        human has approved/rejected the tool call `run()` paused on above
        (Sprint 4 Phase 3, S-11). The Factory Runtime's existing
        /api/v1/hitl/reviews/{review_id}/approve|reject endpoints are what
        actually record the decision — this only reads it back."""
        run_id = str(uuid.uuid4())
        started = time.perf_counter()
        decision = await self.tool_approval.get_decision(review_id)

        if decision.status in ("pending", "not_found"):
            return {
                "response": _APPROVAL_STILL_PENDING_MESSAGE,
                "session_id": "",
                "run_id": run_id,
                "hitl_pending": True,
                "approval_review_id": review_id,
                "approval_status": decision.status,
                "latency_ms": int((time.perf_counter() - started) * 1000),
                "input_tokens": 0,
                "output_tokens": 0,
            }

        context: dict[str, Any] = (
            json.loads(decision.resume_context) if decision.resume_context else {}
        )
        session_id = context.get("session_id", "")

        # Atomic claim — prevents a racing/retried resume call from
        # invoking a DESTRUCTIVE tool a second time (see tool_approval.py).
        # Once claimed, this review is locked to whatever outcome we
        # return below regardless of any later change to its DB status.
        claimed = await self.tool_approval.try_claim_resume(review_id)
        if not claimed:
            return {
                "response": _APPROVAL_ALREADY_PROCESSED_MESSAGE,
                "session_id": session_id,
                "run_id": run_id,
                "hitl_pending": False,
                "approval_review_id": review_id,
                "approval_status": "already_processed",
                "latency_ms": int((time.perf_counter() - started) * 1000),
                "input_tokens": 0,
                "output_tokens": 0,
            }

        if decision.status in ("rejected", "timeout"):
            write_audit_event(
                tenant_id=self.tenant_id,
                event_type=(
                    "human.approval.rejected"
                    if decision.status == "rejected"
                    else "human.approval.timeout"
                ),
                agent_id=self.agent_id,
                principal_id="agent-runtime",
                action=f"invoke:{context.get('tool_id', '')}",
                resource=self.agent_id,
                result="denied",
            )
            return {
                "response": blocked_response_text(),
                "session_id": session_id,
                "run_id": run_id,
                "hitl_pending": False,
                "approval_review_id": review_id,
                "approval_status": decision.status,
                "latency_ms": int((time.perf_counter() - started) * 1000),
                "input_tokens": 0,
                "output_tokens": 0,
            }

        write_audit_event(
            tenant_id=self.tenant_id,
            event_type="human.approval.granted",
            agent_id=self.agent_id,
            principal_id="agent-runtime",
            action=f"invoke:{context.get('tool_id', '')}",
            resource=self.agent_id,
            result="allowed",
        )
        tool_calls = context.get("tool_calls", [])
        tool_results = await self.tools.execute(
            tool_calls, pre_approved_call_id=context.get("approved_call_id")
        )
        final = await self.llm.complete(
            messages=[
                *context.get("initial_messages", []),
                {
                    "role": "assistant",
                    "content": context.get("assistant_content", ""),
                    "tool_calls": tool_calls,
                },
                {"role": "tool", "content": str(tool_results)},
            ]
        )
        record_llm_cost(self.tenant_id, self.agent_id, final.cost_usd or 0.0)
        return await self._finalize(
            final.content,
            session_id=session_id,
            user_id=context.get("user_id"),
            effective_message=context.get("effective_message", ""),
            run_id=run_id,
            started=started,
            total_input_tokens=final.input_tokens or 0,
            total_output_tokens=final.output_tokens or 0,
            approval_review_id=review_id,
            approval_status="approved",
        )

    async def _finalize(
        self,
        final_response: str,
        *,
        session_id: str,
        user_id: str | None,
        effective_message: str,
        run_id: str,
        started: float,
        total_input_tokens: int,
        total_output_tokens: int,
        approval_review_id: str | None = None,
        approval_status: str | None = None,
    ) -> dict[str, Any]:
        """Shared tail — output guardrail, memory save, and the final
        response dict. LLM cost is recorded by the caller immediately
        after each llm.complete() call, not here (see run()'s own comment
        on why). Used by both a normal turn's completion (run()) and a
        tool-call-approval resume (resume_after_approval(), Sprint 4 Phase
        3/S-11)."""
        output_check = await self.guardrail.check(final_response, source="OUTPUT")
        if output_check.blocked:
            logger.warning(
                "run_output_blocked_by_guardrail", run_id=run_id, reason=output_check.reason
            )
            write_audit_event(
                tenant_id=self.tenant_id,
                event_type="guardrail.output_blocked",
                agent_id=self.agent_id,
                principal_id="agent-runtime",
                action="guardrail_check:OUTPUT",
                resource=self.agent_id,
                result="denied",
                extra={"reason": output_check.reason},
            )
            final_response = blocked_response_text()
        elif output_check.sanitised_text is not None:
            final_response = output_check.sanitised_text

        await self.memory.save(
            session_id=session_id,
            user_id=user_id,
            message=effective_message,
            response=final_response,
        )

        logger.info("run_completed", run_id=run_id, agent_id=self.agent_id)

        result: dict[str, Any] = {
            "response": final_response,
            "session_id": session_id,
            "run_id": run_id,
            "hitl_pending": False,
            "latency_ms": int((time.perf_counter() - started) * 1000),
            "input_tokens": total_input_tokens,
            "output_tokens": total_output_tokens,
        }
        if approval_review_id is not None:
            result["approval_review_id"] = approval_review_id
        if approval_status is not None:
            result["approval_status"] = approval_status
        return result
