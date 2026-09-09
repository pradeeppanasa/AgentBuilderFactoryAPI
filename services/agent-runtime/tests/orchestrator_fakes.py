"""Component-level fakes for orchestrator.py's control flow — one per
sub-component AgentOrchestrator.__init__ constructs. Each accepts and
ignores whatever kwargs the real constructor takes (**_kwargs) so tests
can monkeypatch the class itself without matching every real signature."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from guardrail import GuardrailResult


@dataclass
class FakeLLMResult:
    content: str
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    cost_usd: float | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None


class FakeLLMClient:
    def __init__(self, responses: list[FakeLLMResult] | None = None, **_kwargs: Any) -> None:
        self._responses = responses or [FakeLLMResult(content="Default response.")]
        self.calls: list[dict[str, Any]] = []

    async def complete(
        self, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None = None
    ) -> FakeLLMResult:
        self.calls.append({"messages": messages, "tools": tools})
        index = min(len(self.calls) - 1, len(self._responses) - 1)
        return self._responses[index]


class FakeRAGClient:
    def __init__(self, context: str = "", **_kwargs: Any) -> None:
        self._context = context
        self.retrieve_calls: list[str] = []

    async def retrieve(self, query: str) -> str:
        self.retrieve_calls.append(query)
        return self._context


class FakeToolExecutor:
    def __init__(
        self,
        tool_results: list[dict[str, Any]] | None = None,
        raise_approval_required_for: str | None = None,
        **_kwargs: Any,
    ) -> None:
        self._tool_results = tool_results or []
        # Sprint 4 Phase 3 (S-11) — raises ApprovalRequiredError on the
        # FIRST execute() call only, unless that call is itself the
        # pre-approved resume (matching the real ToolExecutor's
        # pre_approved_call_id semantics) so a resume test can reuse the
        # same fake for both halves of the flow.
        self._raise_approval_required_for = raise_approval_required_for
        self.execute_calls: list[dict[str, Any]] = []

    def get_definitions(self) -> list[dict[str, Any]]:
        return []

    async def execute(
        self, tool_calls: list[dict[str, Any]], pre_approved_call_id: str | None = None
    ) -> list[dict[str, Any]]:
        self.execute_calls.append(
            {"tool_calls": tool_calls, "pre_approved_call_id": pre_approved_call_id}
        )
        if self._raise_approval_required_for and pre_approved_call_id is None:
            from tool_policy import ApprovalRequiredError

            raise ApprovalRequiredError(self._raise_approval_required_for)
        return self._tool_results


class FakeMemoryManager:
    def __init__(self, context: str = "", **_kwargs: Any) -> None:
        self._context = context
        self.save_calls: list[dict[str, Any]] = []

    async def load(self, session_id: str, user_id: str | None) -> str:
        return self._context

    async def save(self, session_id: str, user_id: str | None, message: str, response: str) -> None:
        self.save_calls.append(
            {"session_id": session_id, "user_id": user_id, "message": message, "response": response}
        )


class FakeGuardrailChecker:
    def __init__(
        self,
        input_result: GuardrailResult | None = None,
        output_result: GuardrailResult | None = None,
        **_kwargs: Any,
    ) -> None:
        self._input_result = input_result or GuardrailResult(blocked=False)
        self._output_result = output_result or GuardrailResult(blocked=False)
        self.check_calls: list[tuple[str, str]] = []

    async def check(self, text: str, source: str = "INPUT") -> GuardrailResult:
        self.check_calls.append((text, source))
        return self._input_result if source == "INPUT" else self._output_result


class FakeHITLManager:
    def __init__(self, pause: bool = False, **_kwargs: Any) -> None:
        self._pause = pause
        self.create_review_calls: list[dict[str, Any]] = []

    async def pre_check(self, message: str, context: str) -> dict[str, Any]:
        return (
            {"pause": self._pause, "trigger_condition": "high_risk_decision"}
            if self._pause
            else {"pause": False}
        )

    async def create_review(self, **kwargs: Any) -> str:
        self.create_review_calls.append(kwargs)
        return "HITL-FAKE1234"


@dataclass
class FakeApprovalDecision:
    status: str
    review_id: str
    resume_context: str | None


class FakeToolApprovalManager:
    """Sprint 4 Phase 3 (S-11) — stands in for tool_approval.py's
    ToolApprovalManager. `decision_status` controls what get_decision()
    returns for resume_after_approval() tests; request_approval() always
    "succeeds" and records what it was asked to persist."""

    def __init__(
        self,
        decision_status: str = "pending",
        resume_context: str | None = None,
        claim_succeeds: bool = True,
        **_kwargs: Any,
    ) -> None:
        self._decision_status = decision_status
        self._resume_context = resume_context
        self._claim_succeeds = claim_succeeds
        self.request_approval_calls: list[dict[str, Any]] = []
        self.claim_calls: list[str] = []

    async def request_approval(
        self, tool_id: str, run_id: str, session_id: str, resume_context: str
    ) -> str:
        self.request_approval_calls.append(
            {
                "tool_id": tool_id,
                "run_id": run_id,
                "session_id": session_id,
                "resume_context": resume_context,
            }
        )
        return "TAPR-FAKE1234"

    async def get_decision(self, review_id: str) -> FakeApprovalDecision:
        return FakeApprovalDecision(
            status=self._decision_status, review_id=review_id, resume_context=self._resume_context
        )

    async def try_claim_resume(self, review_id: str) -> bool:
        self.claim_calls.append(review_id)
        return self._claim_succeeds
