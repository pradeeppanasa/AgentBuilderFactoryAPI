"""Guardrail policy + multi-layer decision models (CLAUDE.md Section 37.7,
2026-08-16 expansion — full 3-layer config: BERT sub-checks, Bedrock content
filters, PII/topics/keywords/compliance, blocked messages).

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

Section 37.15 status (2026-08-16): all four Layer 1 sub-checks
(`check_toxicity`/`check_nsfw`/`check_prompt_injection`/`check_gibberish`)
now have a real local ONNXBertClassifier instance wired up in
GuardrailEngine.check_input — one classifier per check, each pointed at
its own model directory (see the DEFAULT_*_MODEL constants below). Only
`check_toxicity` participates in the escalate-to-Bedrock flow (it alone
has an `escalate_threshold`); the other three are single-threshold local
block/pass checks per Section 37.7's schema (they don't get an
`escalate_threshold` field, by design).

Still schema-only, no engine enforcement yet: `TopicConfig.allowed_topics`
(Bedrock's topic policy is DENY-only, no whitelist concept) and
`ComplianceConfig` (an LLM-judge-at-invocation-time concept — Generated
Agent Runtime, F8 — not GuardrailEngine's job).
"""

from __future__ import annotations

from enum import Enum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

GuardrailLayer = Literal["bert", "bedrock", "output"]
GuardrailAction = Literal["block", "escalate", "pass"]

# F9/R39-style floor — admins can tighten, never loosen past this (Section
# 37.7: "Developers cannot set bert_block_threshold below 0.70 or
# bert_escalate_threshold above 0.60"). Enforced in the API layer, not here.
MIN_BERT_BLOCK_THRESHOLD = 0.70
MAX_BERT_ESCALATE_THRESHOLD = 0.60

# Layer 1 model directories (see bert_classifier.py's module docstring) —
# BertConfig intentionally has no per-policy model selector field (Section
# 37.7 doesn't expose one; model choice is Panasa-curated, not
# admin-configurable). These are directory-name conventions under
# GUARDRAILS_BERT_MODEL_DIR, never fetched by this Runtime — the customer
# supplies the actual ONNX export (built from whichever real HF checkpoint
# they choose; the names below are suggestions matching well-known public
# models with a matching task, not a hard requirement).
DEFAULT_TOXICITY_MODEL = "unitary/toxic-bert"
DEFAULT_NSFW_MODEL = "eliasalbouzidi/distilbert-nsfw-text-classifier"
DEFAULT_PROMPT_INJECTION_MODEL = "protectai/deberta-v3-base-prompt-injection-v2"
DEFAULT_GIBBERISH_MODEL = "madhurjindal/autonlp-Gibberish-Detector"


class BertConfig(BaseModel):
    """Layer 1 — local inference, runs inside the VPC (R30), ~50ms."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    block_threshold: float = 0.85
    escalate_threshold: float = 0.40

    check_toxicity: bool = True
    check_nsfw: bool = True
    nsfw_threshold: float = 0.80
    nsfw_validation: Literal["sentence", "full_text"] = "sentence"
    check_prompt_injection: bool = True
    prompt_injection_threshold: float = 0.30
    check_gibberish: bool = True
    gibberish_threshold: float = 0.50
    gibberish_validation: Literal["sentence", "full_text"] = "sentence"


class BedrockStrength(str, Enum):
    NONE = "NONE"
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"


class BedrockFilterConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    input_strength: BedrockStrength = BedrockStrength.HIGH
    output_strength: BedrockStrength = BedrockStrength.HIGH


def _prompt_attack_default() -> BedrockFilterConfig:
    # Prompt attacks are an input-only concept — there is no "output" to
    # classify as a prompt attack.
    return BedrockFilterConfig(output_strength=BedrockStrength.NONE)


class BedrockContentFilters(BaseModel):
    model_config = ConfigDict(extra="forbid")

    sexual_content: BedrockFilterConfig = Field(default_factory=BedrockFilterConfig)
    violence: BedrockFilterConfig = Field(default_factory=BedrockFilterConfig)
    hate_speech: BedrockFilterConfig = Field(default_factory=BedrockFilterConfig)
    insults: BedrockFilterConfig = Field(default_factory=BedrockFilterConfig)
    misconduct: BedrockFilterConfig = Field(default_factory=BedrockFilterConfig)
    prompt_attack: BedrockFilterConfig = Field(default_factory=_prompt_attack_default)


class PiiAction(str, Enum):
    DISABLED = "DISABLED"
    BLOCK = "BLOCK"
    REDACT = "REDACT"


class PiiFieldConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: PiiAction = PiiAction.DISABLED
    applies_to: Literal["input_output", "input_only"] = "input_output"


class PiiConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    credit_card: PiiFieldConfig = Field(default_factory=PiiFieldConfig)
    email: PiiFieldConfig = Field(default_factory=PiiFieldConfig)
    phone: PiiFieldConfig = Field(default_factory=PiiFieldConfig)
    person_name: PiiFieldConfig = Field(default_factory=PiiFieldConfig)
    ssn: PiiFieldConfig = Field(default_factory=PiiFieldConfig)
    ip_address: PiiFieldConfig = Field(default_factory=PiiFieldConfig)
    api_key_secret: PiiFieldConfig = Field(default_factory=PiiFieldConfig)
    date_time: PiiFieldConfig = Field(default_factory=PiiFieldConfig)


class TopicConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # Bedrock's topic name pattern is `[0-9a-zA-Z-_ !?.]+`, max 100 chars
    # (verified against botocore's GuardrailTopicName shape) — not
    # strictly validated here; a topic outside that charset/length simply
    # fails at CreateGuardrail time with a clear AWS error, rather than
    # this module maintaining a duplicate regex of AWS's own constraint.
    banned_topics: list[str] = Field(default_factory=list)
    allowed_topics: list[str] | None = None
    """No Bedrock equivalent (GuardrailTopicType is DENY-only — there is no
    ALLOW/whitelist concept in the real API). Stored/validated here but
    never sent to Bedrock and not yet enforced by GuardrailEngine — same
    disclosed-gap treatment as BertConfig's check_nsfw/check_prompt_injection/
    check_gibberish above."""


class KeywordPatternType(str, Enum):
    LITERAL = "LITERAL"
    REGEX = "REGEX"


class KeywordAction(str, Enum):
    BLOCK = "BLOCK"
    REDACT = "REDACT"


class KeywordRule(BaseModel):
    model_config = ConfigDict(extra="forbid")

    pattern: str
    pattern_type: KeywordPatternType = KeywordPatternType.LITERAL
    action: KeywordAction = KeywordAction.BLOCK


class KeywordPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    rules: list[KeywordRule] = Field(default_factory=list)


class ComplianceFramework(str, Enum):
    GDPR = "GDPR"
    HIPAA = "HIPAA"
    PCI_DSS = "PCI_DSS"
    MAS = "MAS"
    SOC2 = "SOC2"
    ISO_27001 = "ISO_27001"
    EU_AI_ACT = "EU_AI_ACT"
    CCPA = "CCPA"
    FEDRAMP = "FEDRAMP"


class ComplianceConfig(BaseModel):
    """Custom_rules are enforced by an LLM judge at agent-invocation time —
    a Generated Agent Runtime concern (F8), not GuardrailEngine's job."""

    model_config = ConfigDict(extra="forbid")

    frameworks: list[ComplianceFramework] = Field(default_factory=list)
    custom_rules: list[str] = Field(default_factory=list)
    on_violation: Literal["stop_agent", "flag_only"] = "stop_agent"


class BlockedMessages(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # min/max match Bedrock's GuardrailBlockedMessaging shape (1-500 chars,
    # verified against botocore's bundled model) — validated here so an
    # empty/oversized message fails our own request validation with a
    # clean 422 instead of a raw CreateGuardrail error surfacing later.
    content_blocked: str = Field(
        default="This content has been blocked by the content policy.",
        min_length=1,
        max_length=500,
    )
    compliance_blocked: str = Field(
        default="This request cannot be processed due to compliance requirements.",
        min_length=1,
        max_length=500,
    )


class GuardrailPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    policy_id: str
    tenant_id: str
    name: str
    description: str
    created_at: str
    updated_at: str
    created_by: str  # must be admin email — enforced at API layer

    # Layer 1 — BERT (local, VPC, ~50ms — R30)
    bert: BertConfig = Field(default_factory=BertConfig)

    # Layer 2 — AWS Bedrock Guardrail. bedrock_credential_id is accepted and
    # stored for forward-compatibility with a future multi-account
    # credentials store; today's provisioner always uses the Runtime's own
    # ambient IAM role (same pattern as every other AWS client factory in
    # this codebase — see app/shared/aws_clients.py), never a per-policy
    # credential lookup.
    bedrock_enabled: bool = True
    bedrock_credential_id: str | None = None
    bedrock_guardrail_id: str | None = None  # auto-set by the API on save
    bedrock_guardrail_version: str = "DRAFT"  # auto-set by the API on save
    bedrock_content_filters: BedrockContentFilters = Field(default_factory=BedrockContentFilters)

    # PII protection (input + output)
    pii: PiiConfig = Field(default_factory=PiiConfig)

    # Topic control
    topics: TopicConfig = Field(default_factory=TopicConfig)

    # Keyword policy
    keywords: KeywordPolicy = Field(default_factory=KeywordPolicy)

    # Compliance frameworks
    compliance: ComplianceConfig = Field(default_factory=ComplianceConfig)

    # Blocked message text
    blocked_messages: BlockedMessages = Field(default_factory=BlockedMessages)


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
    """Set on the output path when Bedrock redacts (ANONYMIZE) a `pii.*`
    field — the caller-facing replacement text, still never logged verbatim
    (R30's audit-event path only ever gets `layers`, not this)."""
