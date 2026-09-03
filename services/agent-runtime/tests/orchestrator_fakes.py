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

    async def complete(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None = None) -> FakeLLMResult:
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
    def __init__(self, tool_results: list[dict[str, Any]] | None = None, **_kwargs: Any) -> None:
        self._tool_results = tool_results or []
        self.execute_calls: list[list[dict[str, Any]]] = []

    def get_definitions(self) -> list[dict[str, Any]]:
        return []

    async def execute(self, tool_calls: list[dict[str, Any]]) -> list[dict[str, Any]]:
        self.execute_calls.append(tool_calls)
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
    def __init__(self, input_result: GuardrailResult | None = None, output_result: GuardrailResult | None = None, **_kwargs: Any) -> None:
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
        return {"pause": self._pause, "trigger_condition": "high_risk_decision"} if self._pause else {"pause": False}

    async def create_review(self, **kwargs: Any) -> str:
        self.create_review_calls.append(kwargs)
        return "HITL-FAKE1234"
