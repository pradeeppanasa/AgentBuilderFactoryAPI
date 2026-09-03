"""render_terraform_tfvars — Generic Agent Runtime instruction (2026-09-03).

Verifies the generated terraform.auto.tfvars.json supplies exactly the
variables the resolved modules declare, sourced from PlatformSettingsRecord's
"Settings -> Deployment -> AWS Target" fields and never from Terraform state.
"""

from __future__ import annotations

import json

from app.modules.iac_generator.tfvars import render_terraform_tfvars
from app.modules.platform_settings.models import PlatformSettingsRecord
from app.modules.registry.models import AgentConfiguration, ToolConfig

ALWAYS_ON_MODULES = ["base", "api_gateway", "authentication", "compute", "observability"]


def _tenant_settings(**overrides: object) -> PlatformSettingsRecord:
    defaults: dict[str, object] = {
        "tenant_id": "tenant-a",
        "updated_by": "admin@acme.com",
        "updated_at": "2026-09-03T00:00:00+00:00",
    }
    defaults.update(overrides)
    return PlatformSettingsRecord(**defaults)


def _config(**overrides: object) -> AgentConfiguration:
    defaults: dict[str, object] = {
        "model_id": "anthropic.claude-3-5-sonnet-20241022-v2:0",
        "model_provider": "bedrock",
        "system_prompt": "You are a test agent.",
    }
    defaults.update(overrides)
    return AgentConfiguration(**defaults)


def test_unconfigured_target_renders_null_values() -> None:
    """A tenant who hasn't set an AWS Target yet still gets a valid JSON
    file — null values, not a crash — so the deploy flow can proceed and
    the eventual `terraform plan` failure is a clear "no value for required
    variable", not a Runtime-side exception."""
    tfvars_json = render_terraform_tfvars(
        _tenant_settings(), "eu-west-2", _config(), ALWAYS_ON_MODULES
    )
    values = json.loads(tfvars_json)

    assert values["aws_region"] == "eu-west-2"
    assert values["vpc_id"] is None
    assert values["bedrock_endpoint_cidr"] is None
    assert values["ecs_cluster_arn"] is None
    assert values["subnet_ids"] == []
    assert values["runtime_image"] is None
    assert "opensearch_endpoint_cidr" not in values


def test_configured_target_renders_real_values() -> None:
    tenant_settings = _tenant_settings(
        aws_region="us-east-1",
        agent_vpc_id="vpc-0123456789abcdef0",
        agent_subnet_ids=["subnet-aaa", "subnet-bbb"],
        agent_ecs_cluster_arn="arn:aws:ecs:us-east-1:111122223333:cluster/acme",
        agent_runtime_ecr_registry="111122223333.dkr.ecr.us-east-1.amazonaws.com/agent-runtime",
        bedrock_endpoint_cidr="10.0.1.0/24",
    )

    tfvars_json = render_terraform_tfvars(
        tenant_settings, "eu-west-2", _config(), ALWAYS_ON_MODULES
    )
    values = json.loads(tfvars_json)

    assert values["aws_region"] == "us-east-1"  # tenant setting wins over the env fallback
    assert values["vpc_id"] == "vpc-0123456789abcdef0"
    assert values["subnet_ids"] == ["subnet-aaa", "subnet-bbb"]
    assert values["ecs_cluster_arn"] == "arn:aws:ecs:us-east-1:111122223333:cluster/acme"
    assert values["bedrock_endpoint_cidr"] == "10.0.1.0/24"
    assert values["runtime_image"] == (
        "111122223333.dkr.ecr.us-east-1.amazonaws.com/agent-runtime:latest"
    )


def test_falls_back_to_env_region_when_tenant_has_not_set_one() -> None:
    tfvars_json = render_terraform_tfvars(
        _tenant_settings(aws_region=None), "ap-southeast-2", _config(), ALWAYS_ON_MODULES
    )
    assert json.loads(tfvars_json)["aws_region"] == "ap-southeast-2"


def test_opensearch_cidr_only_emitted_when_rag_module_resolved() -> None:
    tenant_settings = _tenant_settings(opensearch_endpoint_cidr="10.0.2.0/24")

    without_rag = json.loads(
        render_terraform_tfvars(tenant_settings, "eu-west-2", _config(), ALWAYS_ON_MODULES)
    )
    assert "opensearch_endpoint_cidr" not in without_rag

    with_rag = json.loads(
        render_terraform_tfvars(
            tenant_settings, "eu-west-2", _config(), [*ALWAYS_ON_MODULES, "rag"]
        )
    )
    assert with_rag["opensearch_endpoint_cidr"] == "10.0.2.0/24"


def test_tool_cidr_emitted_only_when_endpoint_and_endpoint_cidr_both_set() -> None:
    config = _config(
        tools=[
            ToolConfig(
                tool_id="companies-house",
                tool_name="Companies House",
                executor_type="http",
                endpoint="https://api.companieshouse.gov.uk",
                endpoint_cidr="203.0.113.0/24",
            ),
            ToolConfig(
                tool_id="no-cidr-tool",
                tool_name="No CIDR Tool",
                executor_type="http",
                endpoint="https://api.example.com",
                # endpoint_cidr intentionally left unset.
            ),
            ToolConfig(
                tool_id="internal-tool",
                tool_name="Internal Tool",
                executor_type="lambda",
                # No endpoint at all — no egress variable declared for this one.
            ),
        ]
    )

    values = json.loads(
        render_terraform_tfvars(_tenant_settings(), "eu-west-2", config, ALWAYS_ON_MODULES)
    )

    assert values["tool_companies_house_cidr"] == "203.0.113.0/24"
    assert "tool_no_cidr_tool_cidr" not in values
    assert "tool_internal_tool_cidr" not in values
