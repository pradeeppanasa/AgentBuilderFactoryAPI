"""Backend-authoritative model catalog (CLAUDE.md Section 32.1, R37).

The UI must load its model picker from GET /api/v1/platform/models rather
than hardcoding production model IDs — they're configurable and may change.
This in-code catalog is the "backend" R37 refers to: it lives here, not in
the UI, so updating it never requires a UI release. Providers/IDs mirror
app.services.model_router.PROVIDER_PREFIX exactly.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel

ModelProvider = Literal["bedrock", "azure_openai", "self_hosted"]


class ModelInfo(BaseModel):
    model_id: str
    model_provider: ModelProvider
    display_name: str
    supports_knowledge_base: bool = True


MODEL_CATALOG: list[ModelInfo] = [
    ModelInfo(
        model_id="anthropic.claude-3-5-sonnet-20241022-v2:0",
        model_provider="bedrock",
        display_name="Claude 3.5 Sonnet (Bedrock)",
    ),
    ModelInfo(
        model_id="anthropic.claude-3-5-haiku-20241022-v1:0",
        model_provider="bedrock",
        display_name="Claude 3.5 Haiku (Bedrock)",
    ),
    ModelInfo(
        model_id="amazon.titan-text-premier-v1:0",
        model_provider="bedrock",
        display_name="Amazon Titan Text Premier (Bedrock)",
    ),
    ModelInfo(
        model_id="gpt-4o",
        model_provider="azure_openai",
        display_name="GPT-4o (Azure OpenAI)",
    ),
    ModelInfo(
        model_id="gpt-4o-mini",
        model_provider="azure_openai",
        display_name="GPT-4o mini (Azure OpenAI)",
    ),
    ModelInfo(
        model_id="llama-3-70b",
        model_provider="self_hosted",
        display_name="Llama 3 70B (self-hosted)",
        supports_knowledge_base=False,
    ),
]


def get_model_catalog() -> list[ModelInfo]:
    return MODEL_CATALOG
