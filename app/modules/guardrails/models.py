"""Guardrail policy + multi-layer decision models (CLAUDE_Advanced_Config.md
Section 3.5 / 4.2 / 8).

Distinct from app.modules.registry.models.GuardrailConfig (the older,
inline per-agent boolean-flag config already used by the IaC generator's
`guardrails` Terraform module) and from
app.modules.registry.models.AgentSecurityPolicy (Amendment A1's coarse
guardrail_profile/pii_policy/data_classification labels on the capability
contract). A GuardrailPolicy is the library-managed, admin-authored record
those simpler shapes don't replace — `AgentConfiguration.guardrail_policy_id`
references one of these by id.

R30: nothing here ever carries prompt/response content — only thresholds,
identifiers, and (at decision time) confidence scores.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

GuardrailLayer = Literal["bert", "bedrock", "output"]
GuardrailAction = Literal["block", "escalate", "pass"]

# F9/R39-style floor — admins can tighten, never loosen past this (Section 7:
# "Developers cannot set bert_block_threshold below 0.70 or
# bert_escalate_threshold above 0.60"). Enforced in the API layer, not here.
MIN_BERT_BLOCK_THRESHOLD = 0.70
MAX_BERT_ESCALATE_THRESHOLD = 0.60


class GuardrailPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    policy_id: str
    tenant_id: str
    name: str
    description: str

    # Input guardrails — 3-layer architecture
    input_enabled: bool = True

    # Layer 1: BERT (local inference, runs inside VPC — R30)
    bert_enabled: bool = True
    bert_model: str = "unitary/toxic-bert"
    bert_block_threshold: float = 0.85
    bert_escalate_threshold: float = 0.40

    # Layer 2: Bedrock Guardrail (only called when BERT is unsure)
    bedrock_guardrail_id: str | None = None
    bedrock_guardrail_version: str = "DRAFT"

    # Output guardrails — skip BERT, go direct to Bedrock
    output_enabled: bool = True
    output_pii_redaction: bool = True
    output_pii_entities: list[str] = Field(
        default_factory=lambda: ["NAME", "EMAIL", "PHONE", "SSN", "CREDIT_CARD", "ADDRESS"]
    )
    output_topic_blocklist: list[str] = Field(default_factory=list)
    output_profanity_filter: bool = True
    output_max_tokens: int | None = None

    created_by: str
    created_at: str
    updated_at: str


class GuardrailLayerResult(BaseModel):
    layer: GuardrailLayer
    action: GuardrailAction
    confidence: float | None = None
    reason: str | None = None
    """Human-readable category ("toxicity", "pii:EMAIL", ...) — never the
    prompt/response text itself (R30)."""


class GuardrailDecision(BaseModel):
    blocked: bool
    layers: list[GuardrailLayerResult] = Field(default_factory=list)
    sanitised_text: str | None = None
    """Set on the output path when output_pii_redaction fires — the
    caller-facing replacement text, still never logged verbatim (R30's
    audit-event path only ever gets `layers`, not this)."""
