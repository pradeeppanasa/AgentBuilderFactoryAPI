"""LLM inference via LiteLLM (CLAUDE.md Section 32.1 — R27/R28/R38).

Independent of the Factory Runtime's own app/services/model_router.py by
design (F8: this is a separate deployable service) — same provider-prefix
convention and cross-provider-fallback semantics, zero shared code.

R27: all inference goes through LiteLLM, never a direct boto3
bedrock-runtime call. R38: cross-provider fallback only happens when the
agent's own config explicitly sets fallback_model_string — never implicit.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import litellm

PROVIDER_PREFIX: dict[str, str] = {
    "bedrock": "bedrock/",
    "azure_openai": "azure/",
    "self_hosted": "openai/",
}


class UnsupportedModelProviderError(ValueError):
    def __init__(self, model_provider: str) -> None:
        self.model_provider = model_provider
        super().__init__(
            f"Unsupported model_provider {model_provider!r}; "
            f"expected one of {sorted(PROVIDER_PREFIX)}"
        )


@dataclass(frozen=True)
class LLMResult:
    content: str
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    cost_usd: float | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None


class LLMClient:
    def __init__(
        self,
        model_id: str,
        model_provider: str,
        system_prompt: str,
        temperature: float = 0.3,
        max_tokens: int = 2048,
        fallback_model_string: str | None = None,
    ) -> None:
        if model_provider not in PROVIDER_PREFIX:
            raise UnsupportedModelProviderError(model_provider)
        self._model_string = f"{PROVIDER_PREFIX[model_provider]}{model_id}"
        self._system_prompt = system_prompt
        self._temperature = temperature
        self._max_tokens = max_tokens
        self._fallbacks = (
            [{self._model_string: [fallback_model_string]}] if fallback_model_string else None
        )

    async def complete(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
    ) -> LLMResult:
        """`messages` excludes the system prompt — it's always prepended
        here so every call site (initial turn, tool-result follow-up) gets
        it applied consistently."""
        full_messages = [{"role": "system", "content": self._system_prompt}, *messages]

        response = await litellm.acompletion(
            model=self._model_string,
            messages=full_messages,
            max_tokens=self._max_tokens,
            temperature=self._temperature,
            tools=tools or None,
            num_retries=3,
            fallbacks=self._fallbacks,
        )

        choice = response.choices[0]
        message = choice.message
        tool_calls = [
            {"id": call.id, "name": call.function.name, "arguments": call.function.arguments}
            for call in (getattr(message, "tool_calls", None) or [])
        ]

        try:
            cost: float | None = litellm.completion_cost(completion_response=response)
        except Exception:
            cost = None

        usage = getattr(response, "usage", None)
        input_tokens = getattr(usage, "prompt_tokens", None) if usage is not None else None
        output_tokens = getattr(usage, "completion_tokens", None) if usage is not None else None

        return LLMResult(
            content=message.content or "",
            tool_calls=tool_calls,
            cost_usd=cost,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
        )
