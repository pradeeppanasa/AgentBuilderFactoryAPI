"""Pydantic v2 models for the Agent Registry (CLAUDE.md Section 4 + Amendment A1).

These are the desired-state schemas. R02: Agent Registry = desired config.
Terraform state = deployed infra. Never conflate them here.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.modules.iac_generator.validation_models import IaCValidationReport

AgentType = Literal["standard", "orchestrator"]

# Retired 2026-08-18 (CLAUDE.md Section 38.6 "Design corrections" / Wizard
# Redesign). "conversational" and "task" both collapse to "standard" — the
# distinction was never load-bearing anywhere in the pipeline (IaC, change
# impact, guardrails all branched on knowledge_base/tools/orchestration
# presence, never on this specific value). "multi-step" collapses to
# "orchestrator", its closest surviving meaning.
#
# Retired 2026-08-19 (CLAUDE.md Section 39.1 — "Agent Role = Standard |
# Orchestrator ONLY"): "rag" and "tool_executor" are retired for the same
# reason — an agent's structural role is either a single node ("standard",
# which may or may not have a knowledge_base/tools attached) or a manager of
# sub-agents ("orchestrator"). Whether an agent does retrieval or calls tools
# is a *capability* (kb_id set, tool_instances non-empty), not a role, and
# was never branched on by IaC/change-impact/guardrails either. Both
# collapse to "standard". The wizard's UI-side `WizardAgentType`
# (agent-wizard.ts) was narrowed to the same two values on 2026-08-18, ahead
# of this backend change.
#
# No DynamoDB migration is run for either retirement — normalise_agent_type()
# maps old values to new ones at read time so existing records stay readable
# indefinitely; new writes go straight through the AgentType Literal above,
# which rejects retired values with a 422.
_LEGACY_AGENT_TYPE_MAP: dict[str, AgentType] = {
    "conversational": "standard",
    "task": "standard",
    "multi-step": "orchestrator",
    "rag": "standard",
    "tool_executor": "standard",
}


def normalise_agent_type(value: str) -> str:
    """Map a possibly-retired agent_type value to the current vocabulary.

    Read-time only. Values already in the current vocabulary (or anything
    unrecognised) pass through unchanged — unrecognised values still fail
    loudly via the AgentType Literal when the caller constructs the Pydantic
    model, which is the correct outcome for genuinely corrupt data.
    """
    return _LEGACY_AGENT_TYPE_MAP.get(value, value)


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

ProjectLifecycleStatus = Literal["draft", "published", "deprecated", "archived"]
"""Section 38.11's archival lifecycle. Deliberately a separate axis from
`AgentStatus`/`VersionStatus` above, which drive the 12-stage automated
deployment pipeline (F1/F12) already built and tested across Phases 2-17 —
those are untouched by Section 38. This field is populated only for agents
created/managed through the project-scoped routes
(`/api/v1/projects/{project_id}/agents/...`); flat `/api/v1/agents/...`
agents leave it None and are unaffected."""


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
    # CLAUDE.md Section 43.1 (2026-08-19): links this inline retrieval config
    # to a real panasa-knowledge-bases catalog record. Distinct from
    # AgentConfiguration.kb_id below — that field is the one actually wired
    # to the KB catalog picker in the UI; this one lets KBConfig be
    # self-describing without requiring callers to cross-reference the
    # top-level field.
    kb_id: str | None = None
    kb_name: str | None = None
    s3_bucket: str | None = None
    # Section 43.2: exact S3 path Bedrock's data source crawls recursively —
    # "{tenant_id}/{kb_id}/raw/". Never write to the bucket root.
    s3_prefix: str | None = None
    embedding_model: str = "amazon.titan-embed-text-v2:0"
    chunk_strategy: str = "semantic"  # semantic | fixed | paragraph
    top_k: int = Field(default=5, gt=0)
    reranking_enabled: bool = True

    # Hybrid retrieval pipeline config: BM25/lexical + vector/semantic
    # search -> result fusion -> cross-encoder reranker -> top-K selection
    # -> context filtering. Config-only (F8/R30): the Builder Runtime
    # provisions and stores this alongside the rest of KBConfig — it never
    # runs retrieval itself (app/api/v1/playground.py's _run_kb_retrieval
    # stays a stub). The Generated Agent Runtime is the separate service
    # responsible for actually building and running this pipeline.
    hybrid_mode: bool = False
    """Use hybrid BM25 (lexical) + vector (semantic) search with result
    fusion, instead of vector-only retrieval."""
    fusion_weight: float = Field(default=0.5, ge=0.0, le=1.0)
    """Result-fusion balance between lexical and vector search scores:
    0.0 = pure BM25/lexical, 1.0 = pure vector/semantic, 0.5 = even split.
    Meaningful only when hybrid_mode is True."""
    reranker_model: str | None = None
    """Cross-encoder reranker model identifier applied to fused results
    before top-K selection. None = no reranking pass."""
    filter_threshold: float | None = Field(default=None, ge=0.0, le=1.0)
    """Minimum relevance score a chunk must clear to survive context
    filtering (after reranking, before the LLM sees it). None = no
    filtering — the top-K results are used as-is."""


class ToolConfig(BaseModel):
    tool_id: str
    tool_name: str
    executor_type: str  # http | lambda | sql | mcp | builtin
    endpoint: str | None = None
    endpoint_cidr: str | None = None
    """F6/R17 default-deny egress — the CIDR the generated
    base/network.tf.j2 security group allowlists for this tool's `endpoint`
    (its own `variable "tool_{tool_id}_cidr"`). Only meaningful alongside
    `endpoint`; the admin who wires up an HTTP tool with a real endpoint is
    the one who knows what IP range it resolves to."""
    lambda_arn: str | None = None
    input_schema: dict[str, Any] = Field(default_factory=dict)
    credentials_secret_arn: str | None = None  # Reference ONLY — never the secret value
    connection_id: str | None = None  # Section 21 — links to a ConnectionRecord


class ToolPolicyEntry(BaseModel):
    """Sprint 3 Phase 4 (S-08, R64, CLAUDE.md Section 64.3) — whether THIS
    agent may call a given tool, and at what risk level. Distinct from
    `ToolConfig` above: `tools` says what Lambda/endpoint exists for a
    tool_id; `tool_policies` says whether this agent is allowed to invoke
    it. A tool_id absent from `tool_policies` is denied by default even if
    it's present in `tools` — see ToolPolicyEngine (services/agent-runtime/
    tool_policy.py).

    `auto_approve`/`requires_human_approval` are informational only —
    ToolPolicyEngine derives the actual approval requirement from `risk`
    alone, never from these two flags, so an admin's own notes here can
    never silently drift out of sync with what's actually enforced.

    `scope` (Sprint 3 Phase 7, Section 62.3) further restricts what this
    agent may do with the tool — e.g. "project:PANASA" for a Jira tool,
    mirroring the tool registry's own `allowed_scopes` (Section 62.2).
    None means "whatever the tool itself allows" (no additional
    restriction); a wildcard value ("*"/"all"/"admin") is rejected by
    AgentConfigValidator (app/modules/registry/config_validator.py) —
    an agent's tool access must always be an explicit, reviewable scope,
    never "everything this credential can technically do".
    """

    tool: str
    allowed: bool = True
    risk: Literal["LOW", "MEDIUM", "HIGH", "DESTRUCTIVE"] = "LOW"
    auto_approve: bool | None = None
    requires_human_approval: bool | None = None
    scope: str | None = None


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

    # ── Section 38.5 — additive alongside the fields above. sub_agents/
    # is_manager/routing_strategy remain authoritative for the existing A2A
    # routing engine (Section 18/26) and circular-dependency validation
    # (A5) — sub_agent_ids is a simplified id-only list a project-scoped
    # agent can populate without the full SubAgentRef shape.
    parent_orchestrator_id: str | None = None
    sub_agent_ids: list[str] = Field(default_factory=list)
    execution_mode: str | None = None  # e.g. "sync" | "async"
    hitl_enabled: bool = False


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


# ── Advanced Config (CLAUDE_Advanced_Config.md Section 3 / Section 37) ─────
# Additive, not replacing the existing guardrails/knowledge_base/tools/memory/
# output_format fields above — those already drive the IaC generator, the
# IaC validator, and a large existing test suite. The spec's own field list
# (Section 37.11) says these six are what AgentConfiguration gains; nothing
# there says the older fields must be deleted in the same change, and doing
# so would break IaC generation/validation for no requested benefit.


class ModelAdvancedConfig(BaseModel):
    temperature: float = 0.7
    top_p: float = 0.9
    max_output_tokens: int = 2048
    presence_penalty: float = 0.0
    frequency_penalty: float = 0.0
    stop_sequences: list[str] = Field(default_factory=list)
    request_timeout_ms: int = 30000
    retry_count: int = 3
    streaming: bool = False
    conversation_history_turns: int = 10
    max_context_tokens: int = 32000
    fallback_model_string: str | None = None  # R38 — explicit, never implicit
    cost_budget_usd: float | None = None
    latency_budget_ms: int | None = None


class MemoryAdvancedConfig(BaseModel):
    """CLAUDE_Advanced_Config.md Section 37.4 names this class "MemoryConfig"
    — renamed here to avoid colliding with the existing MemoryConfig above
    (a different, already-in-use shape: memory_type/persistent_memory_ttl_days/
    max_session_turns). The AgentConfiguration field name is still
    `memory_config`, matching the spec exactly; only the Python class name
    differs."""

    session_enabled: bool = True
    session_ttl_minutes: int = 60
    long_term_enabled: bool = False
    long_term_max_entries: int = 1000
    long_term_retrieval_top_k: int = 5
    summary_enabled: bool = False
    summary_trigger_turns: int = 10
    summary_model: str | None = None


class ToolInstanceConfig(BaseModel):
    connector_id: str
    timeout_ms: int = 10000
    retry_count: int = 1
    cache_enabled: bool = False
    cache_ttl_seconds: int = 300
    error_handling: str = "fail_request"  # fail_request | skip_tool | use_fallback
    fallback_connector_id: str | None = None
    parallel_calls_allowed: bool = True


class OutputSchemaConfig(BaseModel):
    format: str = "none"  # none | json | xml | markdown
    schema_definition: dict[str, Any] | None = None
    strict_mode: bool = True
    max_retries: int = 2
    fallback_on_max_retries: str = "return_error"  # return_raw | return_error


# ── Section 38.5 — Advanced Agent Configuration (Projects) ─────────────────
# Additive, same treatment as the Section 37 block above: nothing here
# replaces an existing field or model.


class TriggerConfig(BaseModel):
    """How a project-scoped agent can be invoked. Distinct from the
    infrastructure-backed ScheduleRecord/TriggerRecord (Section 19,
    EventBridge-backed) — this is a declarative summary carried on the
    agent's own configuration, not a provisioned AWS resource."""

    trigger_type: str = "manual"  # manual | schedule | webhook | event
    schedule_cron: str | None = None
    webhook_enabled: bool = False


class AccessConfig(BaseModel):
    """Who can see/use a project-scoped agent."""

    visibility: str = "private"  # private | project | public
    allowed_user_emails: list[str] = Field(default_factory=list)
    allowed_roles: list[str] = Field(default_factory=list)


class HitlConfig(BaseModel):
    """Routes agent invocations needing a human decision into the
    panasa-hitl-reviews queue (Section 38.8). Distinct from the existing
    `HumanReviewConfig` above (Section 4.3/8), which drives the Step
    Functions + SNS "human_loop" IaC module for the automated deployment
    pipeline's own approval gates — this is the newer, simpler, in-app
    review queue a project-scoped agent's *runtime invocations* can opt
    into. Both coexist; neither replaces the other."""

    enabled: bool = False
    trigger_conditions: list[str] = Field(default_factory=list)
    reviewer_emails: list[str] = Field(default_factory=list)
    timeout_hours: int = 24
    threshold: float | None = Field(default=None, ge=0.0, le=1.0)
    """Section 46.3's wizard "Threshold" field (e.g. 0.80) — a numeric score
    a run must meet or exceed to trip this HITL gate. None = threshold-based
    triggering isn't used; trigger_conditions alone decide."""


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

    # Tool Policy Engine (Sprint 3 Phase 4, S-08, R64) — default-deny; a
    # tool_id absent from this list cannot be invoked regardless of
    # whether it is configured in `tools` above.
    tool_policies: list[ToolPolicyEntry] = Field(default_factory=list)

    # Human-in-the-Loop
    human_review: HumanReviewConfig | None = None

    # Limits
    token_budget_daily: int | None = None
    rate_limit_rpm: int | None = None
    # Sprint 3 Phase 9 (S-03, R70, CLAUDE.md Section 67.4/67.5) — the UI
    # wizard's AccessConfig (panasa-agent-builder-ui/src/types/
    # agent-wizard.ts) already collects these two flat, top-level fields
    # under these exact names; they had nowhere to persist to until now.
    # Enforcement lives entirely in the separate Generated Agent Runtime
    # (services/agent-runtime/quota.py, F8) — this Runtime only stores the
    # configured limits. rate_limit_rpm/rpd are enforced via Redis atomic
    # counters (fail-open, R29/R39/R70); monthly_budget_usd is checked
    # against AgentRecord.current_month_spend_usd (durable DynamoDB state,
    # fail-closed — see that field's own docstring).
    rate_limit_rpd: int | None = None
    monthly_budget_usd: float | None = None

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

    # ── Advanced Config (Section 37.11) — additive, see the comment above
    # ModelAdvancedConfig for why these coexist with the older fields above.
    kb_id: str | None = None
    guardrail_policy_id: str | None = None
    model_advanced: ModelAdvancedConfig | None = None
    memory_config: MemoryAdvancedConfig | None = None
    tool_instances: list[ToolInstanceConfig] = Field(default_factory=list)
    output_schema: OutputSchemaConfig | None = None

    # ── Section 38.5 (Advanced Agent Configuration / Projects) ──────────
    # "Identity" fields (name/description/agent_type/tags/project_id/
    # owner_email) already live on AgentRecord, not here — see
    # AgentRecord.project_id/owner_email instead of duplicating them onto
    # AgentConfiguration. "Versioning" fields (version/status/changelog)
    # likewise map onto AgentRecord.current_version /
    # AgentRecord.project_lifecycle_status / AgentVersionRecord.
    # change_description — no new fields needed for those either.

    # Persona
    persona_name: str | None = None
    greeting_message: str | None = None
    response_tone: str | None = None  # e.g. professional | friendly | technical

    # Conversation
    max_turns: int | None = None
    session_timeout_minutes: int | None = None

    # Skill catalog references (Section 38.3's reusable prompt-capability
    # Skill, panasa-skills) — distinct from `skills: list[SkillConfig]`
    # above (Section 29's built-in platform capabilities, e.g.
    # code_execution/web_search).
    skill_ids: list[str] = Field(default_factory=list)

    trigger: TriggerConfig | None = None
    access: AccessConfig | None = None
    hitl: HitlConfig | None = None


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

    # ── Section 38 (Projects) — additive. None for every agent created via
    # the flat /api/v1/agents routes (Section 5.1); populated only for
    # agents created/managed via /api/v1/projects/{project_id}/agents/...
    project_id: str | None = None
    project_lifecycle_status: ProjectLifecycleStatus | None = None
    """Section 38.11's draft/published/deprecated/archived lifecycle — a
    separate axis from `status` above (which drives the unrelated 12-stage
    deployment pipeline, F1/F12). See the ProjectLifecycleStatus docstring."""
    owner_email: str | None = None  # Section 38.5 — business owner

    # ── Sprint 3 Phase 8 (S-02, R60-R63, Section 64.4) ──────────────────
    # The generated agent runtime's own auth layer (services/agent-runtime/
    # auth.py, ApiKeyAuthProvider — Phase 1, a separate service, F8) reads
    # these five fields directly off this record. This Runtime never sees
    # or stores the raw key value (R61) — only Secrets Manager ARNs.
    api_key_secret_arn: str | None = None
    """None until an initial key is provisioned (create_agent, prototype
    mode only — R60: in enterprise mode the key is generated by Terraform's
    random_password inside the customer VPC, never by this Runtime)."""
    previous_api_key_secret_arn: str | None = None
    """Set only during the 24h dual-key rotation grace window (Section
    64.4) — both this and api_key_secret_arn validate during that window."""
    previous_key_expires_at: int | None = None
    """Unix epoch seconds — NOT ISO 8601, unlike every other timestamp on
    this model. Must match services/agent-runtime/auth.py's
    `time.time() < previous_key_expires_at` comparison exactly; changing
    this to a string would silently break that cross-service contract."""
    rotated_at: str | None = None
    api_key_revoked: bool = False
    """Checked before any secret value comparison (services/agent-runtime/
    auth.py) — a revoked key is denied immediately, even within its own
    5-minute value-cache TTL."""

    # ── Sprint 4 Phase 4 (S-12, CLAUDE.md Section 63.1) ─────────────────
    # Per-agent trust config for services/agent-runtime/auth.py's
    # JwtAuthProvider — a completely different trust root from this
    # Runtime's OWN Factory Console user auth (app/modules/auth/,
    # jwt_secret_arn in app/config.py), which signs and verifies its own
    # HS256 tokens against one shared secret. These fields instead let a
    # CUSTOMER's own OAuth2/OIDC identity provider authenticate business
    # apps calling the generated agent directly. Flat fields on this
    # record (not AgentConfiguration) for the same reason as the API-key
    # block above: services/agent-runtime/config_loader.py's
    # get_current_agent_record() reads only this table, fresh, on every
    # request (R67) — AgentConfiguration is a different table, loaded
    # once at startup. An agent with jwt_issuer or jwt_jwks_url unset has
    # JWT auth off entirely; JwtAuthProvider always defers in that case.
    jwt_issuer: str | None = None
    jwt_audience: str | None = None
    """None means "don't check audience" — some customer IdPs don't issue
    an aud claim meaningful to a downstream API; this is deliberately
    less strict than requiring one, in favour of jwt_issuer + JWKS-pinned
    signature verification remaining mandatory either way."""
    jwt_jwks_url: str | None = None
    """Must be HTTPS — JwtAuthProvider rejects an http:// value outright
    rather than attempting the fetch."""
    jwt_tenant_claim: str = "tenant_id"
    """Which claim in the verified token carries this business app's
    tenant_id — some IdPs use a custom/namespaced claim name (e.g.
    Auth0-style "https://myapp.com/org_id"). The extracted value is
    trusted as-is; services/agent-runtime/main.py's existing R67
    tenant-mismatch check (comparing it against THIS record's own
    tenant_id) is what actually enforces it belongs here."""

    # ── Sprint 3 Phase 9 (S-03, R70, Section 67.3) ──────────────────────
    # Durable running spend total, updated by the Generated Agent Runtime
    # via an atomic DynamoDB update after every /chat request's real
    # litellm cost_usd (services/agent-runtime/quota.py) — never Redis
    # (R29: Redis is never the source of truth for durable data). Checked
    # against AgentConfiguration.monthly_budget_usd BEFORE each request;
    # enforcement is necessarily soft (the actual cost of a request is
    # only known after the LLM call returns), so this can block the
    # request *after* the one that pushed spend over budget, never that
    # one itself — see Section 67.3's own docstring for why that's stated
    # explicitly rather than assumed.
    current_month_spend_usd: float = 0.0
    current_month_spend_period: str | None = None
    """"YYYY-MM" (UTC) of the period current_month_spend_usd covers. No
    separate scheduled reset job exists (out of this phase's scope,
    Section 67.3) — instead the Generated Agent Runtime's atomic update
    lazily resets the total to just that request's own cost the first
    time it sees a period that doesn't match this value, rather than
    accumulating forever. That reset itself isn't concurrency-hardened
    against multiple requests racing across the exact month boundary —
    documented, accepted tradeoff for a "basic implementation" quota
    system (Section 67.3's own framing), and the only failure direction
    is under-counting for a few concurrent requests once a month, never
    a false block."""


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

    # Sprint 4 Phase 5 (S-13b, CLAUDE.md Section 62.4/R68) — SHA-256 of
    # `configuration`'s canonical JSON (app/shared/config_hash.py),
    # computed once at build_version() time and never touched by
    # record_derived_fields() — immutable, like configuration itself.
    # `| None` only for backward compatibility with version records
    # written before this field existed; every version created from now
    # on always has one. services/agent-runtime's generated
    # compute.tf.j2 bakes this same value in as the AGENT_CONFIG_HASH env
    # var; config_loader.py recomputes it independently from the raw
    # dict it loads and refuses to start on a mismatch (R68's "refuse to
    # start, write alert event").
    config_hash: str | None = None

    # Capability contract (Amendment A1, R11) — mandatory, auto-generated
    capability_contract: AgentCapabilityContract

    # Derived artifacts (populated after pipeline runs — later phases)
    iac_version: str | None = None
    iac_s3_key: str | None = None
    iac_modules: list[str] | None = None
    """Resolved module list from the most recent generate-iac call — lets
    GET /agents/{id}/iac/status (Wizard Redesign QA A-04) report per-module
    stages without re-resolving them from the configuration."""
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

    # Section 38.11 — per-version draft/published/deprecated/archived
    # status for project-scoped agents (None for flat /api/v1/agents
    # versions). Mutated via AgentRegistryStore.publish_agent/archive_agent/
    # rollback_project_agent — a mutable derived field like iac_version
    # etc. above, not part of the immutable configuration snapshot.
    project_lifecycle_status: ProjectLifecycleStatus | None = None
