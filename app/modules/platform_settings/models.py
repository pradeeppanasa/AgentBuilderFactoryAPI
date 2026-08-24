"""Platform-wide tenant settings (CLAUDE.md Section 39/R45, R45-7/8; Section
45.3/45.13's `deployment.approval_mode`, added 2026-08-22).

One record per tenant. Secret values (Langfuse secret key, Datadog API key)
are NEVER stored here — only the Secrets Manager ARN they were written to
(R11: "Never store secret values in DynamoDB — store ARN references only").
The API layer is responsible for writing the value to Secrets Manager and
persisting only the returned ARN via this model.

Started as observability-only (Section 39); additive since — same treatment
every other "Settings → X" tenant preference gets rather than a new table
per settings page.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from app.modules.deployment.models import ApprovalMode, CICDProvider

# Fixed range-key value — one settings item per tenant, so the table's
# hash+range schema (matching every other table in bootstrap/stage1/
# dynamodb.tf) still applies without introducing a real per-item id.
GLOBAL_SETTING_ID = "GLOBAL"


class PlatformSettingsRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tenant_id: str
    setting_id: str = GLOBAL_SETTING_ID

    # OTel collector endpoint — customer's own collector, never Panasa's.
    otel_endpoint: str | None = None

    # Langfuse (optional, customer-routed — R45).
    langfuse_enabled: bool = False
    langfuse_public_key: str | None = None
    langfuse_secret_arn: str | None = None  # Secrets Manager ARN only
    langfuse_host: str | None = None

    # Datadog (optional, customer-routed — R45).
    datadog_enabled: bool = False
    datadog_api_key_arn: str | None = None  # Secrets Manager ARN only
    datadog_site: str | None = None

    # Grafana / Loki (optional, customer-routed — R45/41.5). Endpoint-only:
    # like the generic OTel endpoint above, Grafana/Loki accepts spans over
    # plain OTLP with no separate API key for basic wiring.
    grafana_enabled: bool = False
    grafana_endpoint: str | None = None

    # New Relic (optional, customer-routed — R45/41.5). Same shape as
    # Datadog: needs a license/API key, not just an endpoint.
    new_relic_enabled: bool = False
    new_relic_api_key_arn: str | None = None  # Secrets Manager ARN only

    # Dynatrace (optional, customer-routed — R45/41.5). Endpoint-only, same
    # shape as Grafana/Loki — matches Section 41.5's UI mockup exactly.
    dynatrace_enabled: bool = False
    dynatrace_endpoint: str | None = None

    # Deployment (Section 45.3/45.13, R50) — "Settings → Deployment". Default
    # "automated" keeps F1's fully-automated pipeline exactly as frozen;
    # a tenant that wants R50/Stage 5's human-approval gate opts in here.
    # New deployments read this once, at trigger time, onto their own
    # DeploymentRecord.approval_mode — changing this setting never affects
    # a deployment already in flight.
    default_approval_mode: ApprovalMode = "automated"

    # Section 45.6/R58 — "Settings → Deployment → CI/CD Provider". Read once
    # per agent's first deploy (Section 45.2, when its repo is created) to
    # pick which workflow file iac_generator/cicd_templates.py commits;
    # changing this later never rewrites an existing repo's workflow file.
    cicd_provider: CICDProvider = "github_actions"

    updated_by: str
    updated_at: str
