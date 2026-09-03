"""Terraform variable values for a generated agent's deploy (2026-09-03,
Generic Agent Runtime instruction).

Every per-module `.tf.j2` template (compute, base/network, …) already
declares its own required `variable` blocks with no defaults — that was
true before this module existed too, it just had no way to ever be filled
in, so `terraform plan`/`apply` in the customer's CI/CD would always have
failed asking for them interactively. This renders a
`terraform.auto.tfvars.json` (Terraform auto-loads any `*.auto.tfvars.json`
file in the working directory — no `-var-file` flag needed in the
generated CI/CD workflow) supplying real values for exactly the variables
the resolved modules for THIS agent actually declare.

Values come from two places, deliberately never Terraform state (R03/F0):
  - PlatformSettingsRecord's "Settings -> Deployment -> AWS Target" fields
    (agent_vpc_id, agent_subnet_ids, agent_ecs_cluster_arn,
    agent_runtime_ecr_registry, bedrock_endpoint_cidr,
    opensearch_endpoint_cidr) — a pre-existing customer VPC/ECS
    cluster/ECR repo the tenant configures once, same "None means not
    configured yet" convention as kb_s3_bucket.
  - The agent's own AgentConfiguration (per-tool `endpoint_cidr`, for the
    egress-allowlist variable base/network.tf.j2 declares per tool with a
    real `endpoint`).

Regenerated on every deploy (unlike the CI/CD workflow file, which is
committed once) — these values are as config-driven as the Terraform
itself, and a tenant correcting a wrong VPC ID must take effect on the
next deploy, not be stuck forever like the workflow file deliberately is.
"""

from __future__ import annotations

import json

from app.modules.platform_settings.models import PlatformSettingsRecord
from app.modules.registry.models import AgentConfiguration


def render_terraform_tfvars(
    tenant_settings: PlatformSettingsRecord,
    fallback_aws_region: str,
    config: AgentConfiguration,
    resolved_modules: list[str],
) -> str:
    """Returns pretty-printed JSON text for terraform.auto.tfvars.json.

    `fallback_aws_region` is the env-level AWS_REGION (app.config.Settings)
    — the same fallback admin_settings.py's GET /deployment already applies
    when the tenant hasn't set their own region.
    """
    values: dict[str, object] = {
        "aws_region": tenant_settings.aws_region or fallback_aws_region,
        "vpc_id": tenant_settings.agent_vpc_id,
        "bedrock_endpoint_cidr": tenant_settings.bedrock_endpoint_cidr,
        "ecs_cluster_arn": tenant_settings.agent_ecs_cluster_arn,
        "subnet_ids": tenant_settings.agent_subnet_ids,
        "runtime_image": _runtime_image(tenant_settings),
    }

    if "rag" in resolved_modules:
        values["opensearch_endpoint_cidr"] = tenant_settings.opensearch_endpoint_cidr

    for tool in config.tools:
        if tool.endpoint and tool.endpoint_cidr:
            key = f"tool_{tool.tool_id.replace('-', '_')}_cidr"
            values[key] = tool.endpoint_cidr

    return json.dumps(values, indent=2, sort_keys=True) + "\n"


def _runtime_image(tenant_settings: PlatformSettingsRecord) -> str | None:
    if not tenant_settings.agent_runtime_ecr_registry:
        return None
    return f"{tenant_settings.agent_runtime_ecr_registry}:latest"
