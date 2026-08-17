"""Runtime settings. Reads all configuration from environment / .env.

R01: DEPLOYMENT_MODE changes only infra targets, never business logic.
"""

from __future__ import annotations

from typing import Literal

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

DeploymentMode = Literal["prototype", "enterprise"]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # ── Deployment mode ──────────────────────────────────────────
    deployment_mode: DeploymentMode = "prototype"

    # ── AWS ───────────────────────────────────────────────────────
    aws_region: str = "eu-west-2"
    aws_account_id: str = "123456789012"

    # ── Agent Registry (DynamoDB) ──────────────────────────────────
    dynamodb_agents_table: str = "panasa-agents"
    dynamodb_versions_table: str = "panasa-agent-versions"
    dynamodb_deployments_table: str = "panasa-deployments"
    dynamodb_connectors_table: str = "panasa-connectors"
    dynamodb_schedules_table: str = "panasa-schedules"
    dynamodb_connections_table: str = "panasa-connections"
    dynamodb_memory_table: str = "panasa-memory"
    dynamodb_mcp_servers_table: str = "panasa-mcp-servers"
    dynamodb_skills_table: str = "panasa-skills"
    dynamodb_transcripts_table: str = "panasa-transcripts"
    dynamodb_reports_table: str = "panasa-reports"

    # ── Advanced Config: KB / Guardrail Policy / Playground libraries
    # (CLAUDE_Advanced_Config.md Section 4/8, Section 37.12) ──────────────
    dynamodb_knowledge_bases_table: str = "panasa-knowledge-bases"
    dynamodb_guardrail_policies_table: str = "panasa-guardrail-policies"
    dynamodb_playground_sessions_table: str = "panasa-playground-sessions"
    # Section 37.15 (2026-08-16) — STS AssumeRole credential bindings for
    # Bedrock guardrail provisioning across accounts.
    dynamodb_bedrock_credentials_table: str = "panasa-bedrock-credentials"

    # ── Projects / Skills / HITL (CLAUDE.md Section 38) ────────────────────
    dynamodb_projects_table: str = "panasa-projects"
    # dynamodb_skills_table (defined above) is repurposed here for Section
    # 38.3's reusable prompt-capability Skill — Section 4.9/29's built-in
    # platform-capability Skill concept was never implemented in this
    # codebase, so there is no real collision, only a shared table name.
    dynamodb_hitl_reviews_table: str = "panasa-hitl-reviews"

    # ── Admin observability settings (Section 39/R45, R45-7/8) ─────────────
    dynamodb_platform_settings_table: str = "panasa-platform-settings"

    # ── Task Planner (Section 38.6/38.7 — A2-3) ────────────────────────────
    # Factory-internal call (Section 5.11/22 rule): uses the Runtime's own
    # Bedrock access, never the generated agent's own model config.
    task_planner_model_id: str = "anthropic.claude-3-5-haiku-20241022-v1:0"
    task_planner_max_tokens: int = 2048

    # ── Deployment pipeline ───────────────────────────────────────
    eventbridge_bus_name: str = "panasa-agent-builder"
    step_functions_arn: str | None = None

    # ── IaC & Git ───────────────────────────────────────────────────
    iac_output_bucket: str | None = None
    iac_tool: Literal["terraform", "cdk"] = "terraform"
    git_provider: Literal["github", "gitlab", "bitbucket", "codecommit"] = "github"
    git_repo_url: str | None = None
    git_credentials_secret: str | None = None

    # ── Terraform backend (enterprise only) ────────────────────────
    tf_state_bucket: str | None = None
    tf_state_lock_table: str | None = None

    # ── Platform ──────────────────────────────────────────────────────
    platform_version: str = "1.0.0"
    runtime_image: str | None = None

    # ── Platform upgrade (Phase 15, Section 5.6/14) ────────────────────
    dynamodb_platform_upgrades_table: str = "panasa-platform-upgrades"
    platform_upgrade_state_machine_arn: str | None = None
    ecs_cluster_name: str | None = None
    ecs_runtime_service_name: str = "agent-builder-runtime"
    ecs_task_definition_family: str | None = None
    platform_health_check_url: str | None = None

    # ── Auth ──────────────────────────────────────────────────────────
    jwt_secret_arn: str | None = None
    jwt_algorithm: str = "HS256"
    jwt_access_token_expire_minutes: int = 30
    jwt_refresh_token_expire_days: int = 30

    # ── Database (Phase 3 — user accounts) ─────────────────────────────
    database_url: str = "postgresql+asyncpg://panasa:panasa@localhost:5432/panasa_agent_builder"

    # ── Secrets Manager ───────────────────────────────────────────────
    secrets_manager_prefix: str = "panasa/agents"

    # ── Telemetry (opt-in, default OFF) ───────────────────────────────
    telemetry_enabled: bool = False
    telemetry_endpoint: str | None = None

    # ── Observability ──────────────────────────────────────────────────
    log_level: str = "INFO"
    # R45: Langfuse is an optional, customer-routed backend — never a
    # required runtime dependency. Explicit flag (rather than gating purely
    # on langfuse_host being set) so `.env.example` documents the opt-in
    # nature directly, matching Section 3/32's LANGFUSE_ENABLED=false.
    langfuse_enabled: bool = False
    langfuse_host: str | None = None
    langfuse_public_key: str | None = None
    langfuse_secret_key: str | None = None
    cloudwatch_metrics_namespace: str = "Panasa/AgentBuilder"

    # ── Audit (Phase 14 — S3 WORM, Section 14) ────────────────────────
    audit_s3_bucket: str | None = None

    # ── License (F11) ────────────────────────────────────────────────
    license_token_secret_arn: str | None = None

    # ── LiteLLM model router (Section 32.1, R27/R28/R37/R38) ──────────
    litellm_log: str = "WARNING"  # DEBUG | INFO | WARNING
    litellm_telemetry: bool = False  # R28 — always disabled, belt-and-suspenders

    # Bedrock uses aws_region above. Azure OpenAI / self-hosted are optional
    # — only required if an agent's model_provider selects them.
    azure_api_key: str | None = None
    azure_api_base: str | None = None
    azure_api_version: str = "2024-02-01"
    openai_api_base: str | None = None  # e.g. http://localhost:11434/v1 (Ollama)
    openai_api_key: str | None = None  # self-hosted placeholder, e.g. "ollama"

    # ── Redis cache layer (Section 32.2, R29/R39) ─────────────────────
    redis_url: str = "redis://localhost:6379/0"
    redis_max_connections: int = 20
    redis_socket_timeout: float = 1.0  # fail fast — never block a request on a cache miss

    # ── Guardrail Layer 1 — local ONNX BERT classifier ────────────────
    # Base directory containing one pre-exported ONNX model per bert_model
    # name, e.g. {guardrails_bert_model_dir}/unitary/toxic-bert/{model.onnx,
    # tokenizer.json, config.json}. Never downloaded at runtime — supplied
    # during image build or via a customer-side artifact mount. None means
    # "no model configured": ONNXBertClassifier raises
    # GuardrailModelUnavailableError on first use, not at import time.
    guardrails_bert_model_dir: str | None = None

    # ── Local dev overrides ───────────────────────────────────────────
    dynamodb_endpoint: str | None = None
    secrets_manager_endpoint: str | None = None  # e.g. http://localstack:4566
    s3_endpoint: str | None = None  # e.g. http://localstack:4566

    @field_validator("deployment_mode", mode="before")
    @classmethod
    def _normalise_deployment_mode(cls, value: str) -> str:
        normalised = str(value).strip().lower()
        if normalised not in {"prototype", "enterprise"}:
            raise ValueError(f"DEPLOYMENT_MODE must be 'prototype' or 'enterprise', got {value!r}")
        return normalised


settings = Settings()
