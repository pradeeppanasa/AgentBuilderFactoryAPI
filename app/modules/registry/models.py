"""Pydantic v2 models for the Agent Registry (CLAUDE.md Section 4 + Amendment A1).

These are the desired-state schemas. R02: Agent Registry = desired config.
Terraform state = deployed infra. Never conflate them here.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.modules.iac_generator.validation_models import IaCValidationReport

AgentType = Literal["conversational", "task", "rag", "multi-step", "orchestrator"]

AgentStatus = Literal[
    "DRAFT",
    "VALIDATING",
    "TESTING",
    "READY_FOR_APPROVAL",
    "APPROVED",
    "DEPLOYING",
    "ACTIVE",
    "FAILED",
    "BLOCKED",
    "ROLLED_BACK",
    "DEPRECATED",
]

VersionStatus = Literal["DRAFT", "TESTING", "BLOCKED", "LIVE", "SUPERSEDED", "ROLLED_BACK"]


# ── Nested configuration (Section 4.3) ──────────────────────────────────────


class GuardrailConfig(BaseModel):
    prompt_injection: bool = True
    pii_detection: bool = True
    toxicity_filter: bool = True
    topic_filter: bool = False
    blocked_topics: list[str] = Field(default_factory=list)
    hallucination_check: bool = True
    pii_strip_output: bool = True


class KBConfig(BaseModel):
    enabled: bool = False
    kb_name: str | None = None
    s3_bucket: str | None = None
    embedding_model: str = "amazon.titan-embed-text-v2:0"
    chunk_strategy: str = "semantic"  # semantic | fixed | paragraph
    top_k: int = 5
    reranking_enabled: bool = True


class ToolConfig(BaseModel):
    tool_id: str
    tool_name: str
    executor_type: str  # http | lambda | sql | mcp | builtin
    endpoint: str | None = None
    lambda_arn: str | None = None
    input_schema: dict[str, Any] = Field(default_factory=dict)
    credentials_secret_arn: str | None = None  # Reference ONLY — never the secret value
    connection_id: str | None = None  # Section 21 — links to a ConnectionRecord


class HumanReviewConfig(BaseModel):
    enabled: bool = False
    trigger_conditions: list[str] = Field(default_factory=list)
    notification_sns_arn: str | None = None
    approval_timeout_hours: int = 24


class MemoryConfig(BaseModel):
    memory_type: str = "none"  # none | session | persistent
    persistent_memory_ttl_days: int = 30
    max_session_turns: int = 50


class OutputFormatConfig(BaseModel):
    format_type: str = "text"  # text | json | markdown | structured
    json_schema: dict[str, Any] | None = None
    output_instructions: str | None = None


class SubAgentRef(BaseModel):
    agent_id: str
    agent_name: str
    capability_description: str
    allowed_actions: list[str] = Field(default_factory=list)


class PipelineStep(BaseModel):
    """Section 26 — used when routing_strategy = 'pipeline'."""

    agent_id: str
    input_mapping: dict[str, Any] = Field(default_factory=dict)
    condition: str | None = None
    on_failure: str = "stop"  # "stop" | "skip" | "use_fallback"


class PipelineConfig(BaseModel):
    steps: list[PipelineStep] = Field(default_factory=list)


class OrchestrationConfig(BaseModel):
    """Managerial Agent — this agent routes tasks to sub-agents."""

    is_manager: bool = False
    routing_strategy: str = "llm"  # llm | rules | round_robin | broadcast | pipeline
    sub_agents: list[SubAgentRef] = Field(default_factory=list)
    fallback_agent_id: str | None = None
    max_delegation_depth: int = 2  # Default 2, maximum 5 (A4)
    pipeline_config: PipelineConfig | None = None


class MCPServerConfig(BaseModel):
    server_id: str
    server_name: str
    transport: str  # sse | http | stdio
    endpoint: str
    credentials_secret_arn: str | None = None
    tool_filter: list[str] = Field(default_factory=list)  # Empty = expose all tools


class SkillConfig(BaseModel):
    skill_id: str  # e.g. "code_execution" | "web_search" | ...
    enabled: bool = True
    config: dict[str, Any] = Field(default_factory=dict)


class AgentConfiguration(BaseModel):
    # Model
    model_id: str
    model_provider: str  # "bedrock" | "azure_openai" | "self_hosted"
    temperature: float = 0.3
    max_tokens: int = 2048
    top_p: float = 0.9
    context_window_k: int = 32
    fallback_model_string: str | None = None
    """R38: explicit LiteLLM fallback target, e.g. "bedrock/anthropic.claude-3-5-haiku-...".
    Cross-provider fallback is allowed only when set here — never implicit."""

    # Prompts
    system_prompt: str
    system_prompt_variables: list[str] = Field(default_factory=list)
    prompt_version: str | None = None

    # Guardrails
    guardrails: GuardrailConfig = Field(default_factory=GuardrailConfig)

    # Policy
    policies: list[str] = Field(default_factory=list)

    # Knowledge Base
    knowledge_base: KBConfig | None = None

    # Tools / Connectors (per-agent isolation — Phase 1 / R21)
    tools: list[ToolConfig] = Field(default_factory=list)

    # Human-in-the-Loop
    human_review: HumanReviewConfig | None = None

    # Limits
    token_budget_daily: int | None = None
    rate_limit_rpm: int | None = None

    # Observability
    observability_enabled: bool = True
    langfuse_enabled: bool = True

    # Audit
    audit_enabled: bool = True
    audit_s3_prefix: str | None = None

    # Memory
    memory: MemoryConfig = Field(default_factory=MemoryConfig)

    # Output Format
    output_format: OutputFormatConfig = Field(default_factory=OutputFormatConfig)

    # A2A Orchestration (Managerial Agent)
    orchestration: OrchestrationConfig | None = None

    # MCP Servers
    mcp_servers: list[MCPServerConfig] = Field(default_factory=list)

    # Skills (built-in platform capabilities)
    skills: list[SkillConfig] = Field(default_factory=list)


# ── Agent Capability Contract (Amendment A1 / R11) ──────────────────────────


class AgentSecurityPolicy(BaseModel):
    guardrail_profile: str = "standard"  # standard | strict | pii_safe | financial | custom
    pii_policy: str = "redact"  # block | redact | allow
    data_classification: str = "internal"  # public | internal | confidential | restricted


class AgentCapabilityContract(BaseModel):
    """Machine-readable contract, auto-generated on version save (R11).

    Single source of truth for orchestrator routing, dependency mapping,
    policy enforcement, evaluation scoping, IaC impact analysis, and the UI.
    """

    agent_id: str
    agent_name: str
    agent_type: AgentType
    version: int
    description: str

    capabilities: list[str] = Field(default_factory=list)

    accepted_input_schema: dict[str, Any] | None = None
    output_schema: dict[str, Any] | None = None

    skills: list[str] = Field(default_factory=list)
    tools: list[str] = Field(default_factory=list)
    mcp_servers: list[str] = Field(default_factory=list)
    knowledge_bases: list[str] = Field(default_factory=list)

    allowed_actions: list[str] = Field(default_factory=lambda: ["read"])
    restricted_actions: list[str] = Field(default_factory=lambda: ["external_write"])
    security_policy: AgentSecurityPolicy = Field(default_factory=AgentSecurityPolicy)

    latency_sla_ms: int | None = None
    token_budget: int | None = None


# ── Placeholder result types (populated by later phases) ───────────────────
# Referenced by AgentVersionRecord per Section 4.2. Full shape is defined by
# the security scanning (Phase 9/12) and evaluation (Phase 10/13) modules.


class SecurityResult(BaseModel):
    passed: bool
    summary: str
    findings: list[dict[str, Any]] = Field(default_factory=list)


class EvaluationResult(BaseModel):
    passed: bool
    summary: str
    scores: dict[str, float] = Field(default_factory=dict)


# ── Registry records (Section 4.1 / 4.2) ────────────────────────────────────


class AgentRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # Keys
    tenant_id: str
    agent_id: str

    # Identity
    name: str
    description: str
    business_purpose: str
    agent_type: AgentType

    # Versioning
    current_version: int
    live_version: int | None = None
    status: AgentStatus = "DRAFT"

    # Platform versions at creation
    platform_version: str
    runtime_version: str

    # Audit
    created_by: str
    created_at: str  # ISO 8601
    updated_by: str
    updated_at: str
    tags: dict[str, str] = Field(default_factory=dict)


class AgentVersionRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # Keys
    agent_id: str
    version: int

    # Status of this specific version
    version_status: VersionStatus = "DRAFT"

    # Change metadata
    change_description: str
    changed_by: str
    created_at: str

    # Full desired configuration snapshot (immutable after creation)
    configuration: AgentConfiguration

    # Capability contract (Amendment A1, R11) — mandatory, auto-generated
    capability_contract: AgentCapabilityContract

    # Derived artifacts (populated after pipeline runs — later phases)
    iac_version: str | None = None
    iac_s3_key: str | None = None
    iac_validation_report: IaCValidationReport | None = None
    """Set only by POST /agents/{id}/generate-iac (CLAUDE.md Section 6's IaC
    validation suite) — not by the deploy flow's own IaC generation, whose
    real gate is the deployment pipeline's own SECURITY_SCANNING/
    TERRAFORM_VALIDATE stages (F0/F2/R05)."""
    deployment_id: str | None = None
    security_result: SecurityResult | None = None
    evaluation_result: EvaluationResult | None = None
    terraform_plan_summary: str | None = None
    deployment_result: str | None = None  # SUCCESS | FAILED | BLOCKED

    # Rollback linkage
    rolled_back_from_version: int | None = None
