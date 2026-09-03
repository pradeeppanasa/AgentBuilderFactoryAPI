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

import uuid
from typing import Any

import structlog

from guardrail import GuardrailChecker, blocked_response_text
from hitl import HITLManager
from llm_client import LLMClient
from memory import MemoryManager
from rag_client import RAGClient
from tool_executor import ToolExecutor

logger = structlog.get_logger()

_HITL_PENDING_MESSAGE = "This request requires human review. You will be notified."


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
        self.tools = ToolExecutor(agent_id=self.agent_id, tools=config.get("tools") or [])

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

    async def run(self, message: str, session_id: str, user_id: str | None = None) -> dict[str, Any]:
        run_id = str(uuid.uuid4())
        logger.info("run_started", run_id=run_id, agent_id=self.agent_id)

        input_check = await self.guardrail.check(message, source="INPUT")
        if input_check.blocked:
            logger.warning("run_blocked_by_guardrail", run_id=run_id, reason=input_check.reason)
            return {
                "response": blocked_response_text(),
                "session_id": session_id,
                "run_id": run_id,
                "hitl_pending": False,
            }
        effective_message = input_check.sanitised_text or message

        memory_context = await self.memory.load(session_id=session_id, user_id=user_id)

        rag_context = await self.rag.retrieve(query=effective_message)

        augmented_message = self._build_prompt(effective_message, memory_context, rag_context)

        hitl_result = await self.hitl.pre_check(message=effective_message, context=augmented_message)
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
            }

        llm_response = await self.llm.complete(
            messages=[{"role": "user", "content": augmented_message}],
            tools=self.tools.get_definitions(),
        )

        if llm_response.tool_calls:
            tool_results = await self.tools.execute(llm_response.tool_calls)
            final = await self.llm.complete(
                messages=[
                    {"role": "user", "content": augmented_message},
                    {"role": "assistant", "content": llm_response.content, "tool_calls": llm_response.tool_calls},
                    {"role": "tool", "content": str(tool_results)},
                ]
            )
            final_response = final.content
        else:
            final_response = llm_response.content

        output_check = await self.guardrail.check(final_response, source="OUTPUT")
        if output_check.blocked:
            logger.warning("run_output_blocked_by_guardrail", run_id=run_id, reason=output_check.reason)
            final_response = blocked_response_text()
        elif output_check.sanitised_text is not None:
            final_response = output_check.sanitised_text

        await self.memory.save(
            session_id=session_id, user_id=user_id, message=effective_message, response=final_response
        )

        logger.info("run_completed", run_id=run_id, agent_id=self.agent_id)

        return {
            "response": final_response,
            "session_id": session_id,
            "run_id": run_id,
            "hitl_pending": False,
        }

    @staticmethod
    def _build_prompt(message: str, memory: str, rag: str) -> str:
        parts = [message]
        if memory:
            parts.insert(0, f"[Previous context]\n{memory}\n")
        if rag:
            parts.insert(0, f"[Relevant knowledge]\n{rag}\n")
        return "\n".join(parts)
