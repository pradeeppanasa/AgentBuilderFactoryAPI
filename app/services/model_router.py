"""Unified LLM model router (CLAUDE.md Section 32.1, R27/R28/R38).

R27: every LLM inference call this Runtime makes MUST go through here —
direct boto3 bedrock-runtime calls are prohibited. Confirmed scope (per user
direction, 2026-08-13): this Builder Runtime performs the invocation itself
in DEPLOYMENT_MODE=prototype (e.g. sandbox test / prompt generation call
sites); this module is the shared primitive those call sites use. It has no
opinion on how DEPLOYMENT_MODE=enterprise's Generated Agent Runtime (a
separate service, F8) invokes its own models.

R38: cross-provider fallback (e.g. azure -> bedrock) only happens when
`AgentConfiguration.fallback_model_string` is explicitly set — never
implicit. R28: LiteLLM's own telemetry is disabled at startup (app.main
lifespan) in addition to the module-level flag here, belt-and-suspenders.
"""

from __future__ import annotations

from dataclasses import dataclass

import litellm

from app.modules.registry.models import AgentConfiguration

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
class TokenUsage:
    input_tokens: int
    output_tokens: int
    total_tokens: int


def _extract_usage(response: object) -> TokenUsage | None:
    usage = getattr(response, "usage", None)
    if usage is None:
        return None
    try:
        return TokenUsage(
            input_tokens=int(usage.prompt_tokens),
            output_tokens=int(usage.completion_tokens),
            total_tokens=int(usage.total_tokens),
        )
    except (AttributeError, TypeError, ValueError):
        return None


async def call_model(
    config: AgentConfiguration,
    messages: list[dict[str, str]],
) -> tuple[str, float | None, TokenUsage | None]:
    """Returns (response_text, cost_or_None, usage_or_None).

    cost is float | None — some providers/models don't return cost data via
    litellm.completion_cost. usage is TokenUsage | None for the same reason
    (not every provider/mock response carries a `.usage` attribute). Neither
    missing value should ever raise — the call must succeed regardless.
    """
    if config.model_provider not in PROVIDER_PREFIX:
        raise UnsupportedModelProviderError(config.model_provider)

    model_string = f"{PROVIDER_PREFIX[config.model_provider]}{config.model_id}"

    fallbacks = None
    if config.fallback_model_string:
        fallbacks = [{model_string: [config.fallback_model_string]}]

    response = await litellm.acompletion(
        model=model_string,
        messages=messages,
        max_tokens=config.max_tokens,
        temperature=config.temperature,
        num_retries=3,
        fallbacks=fallbacks,
    )

    text: str = response.choices[0].message.content

    try:
        cost: float | None = litellm.completion_cost(completion_response=response)
    except Exception:
        cost = None

    return text, cost, _extract_usage(response)
