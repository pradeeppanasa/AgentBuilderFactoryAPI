"""Terraform backend — renders Jinja2 .tf.j2 templates per resolved module.

Output layout: terraform/agents/{agent_id}/{module}__{file}.tf — flat, not
nested per-module subdirectories. `terraform init`/`plan`/`apply` only ever
look at .tf files sitting directly in the working directory they're run
from; they do NOT recurse into subdirectories the way this generated
config's cross-module resource references need (e.g. compute.tf's
`aws_iam_role.agent_execution_role`, defined in authentication.tf, has to
resolve within a single Terraform root module). An earlier nested layout
(terraform/agents/{agent_id}/{module}/{file}.tf) only ever worked for
IaCValidator's own local validation, which flattens into a temporary
directory purely for that subprocess call (validator.py's
_flatten_for_terraform_cli) — the customer's real CI/CD, given the actual
nested repo layout, would have found zero .tf files in its working
directory and failed immediately. This generates the SAME flat layout
IaCValidator already proved works, so what the CI/CD applies for real is
exactly what local validation already checked — see the Generic Agent
Runtime instruction, 2026-09-03.
"""

from __future__ import annotations

from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

from app.config import Settings
from app.modules.iac_generator.backends.base import IaCBackend
from app.modules.iac_generator.naming import (
    alb_name,
    bedrock_guardrail_name,
    opensearch_collection_name,
    target_group_name,
    tool_lambda_name,
    tool_role_name,
)
from app.modules.registry.models import AgentConfiguration

_TEMPLATES_ROOT = Path(__file__).parent.parent / "templates" / "terraform"


class TerraformBackend(IaCBackend):
    tool_name = "terraform"

    def __init__(self, templates_root: Path = _TEMPLATES_ROOT) -> None:
        self._templates_root = templates_root
        self._env = Environment(
            loader=FileSystemLoader(str(templates_root)),
            autoescape=select_autoescape(disabled_extensions=("j2",), default=False),
            trim_blocks=True,
            lstrip_blocks=True,
            keep_trailing_newline=True,
        )
        # QA I-02 — templates call bedrock_guardrail_name(agent_id) instead
        # of interpolating "panasa-{{ agent_id }}-guardrail" literally, so
        # long agent_ids get truncated instead of exceeding AWS's 50-char
        # Bedrock Guardrail name limit.
        self._env.globals["bedrock_guardrail_name"] = bedrock_guardrail_name
        self._env.globals["alb_name"] = alb_name
        self._env.globals["target_group_name"] = target_group_name
        self._env.globals["opensearch_collection_name"] = opensearch_collection_name
        self._env.globals["tool_lambda_name"] = tool_lambda_name
        self._env.globals["tool_role_name"] = tool_role_name

    def render(
        self,
        agent_id: str,
        tenant_id: str,
        version: int,
        config: AgentConfiguration,
        resolved_modules: list[str],
        settings: Settings | None = None,
    ) -> dict[str, str]:
        context = {
            "agent_id": agent_id,
            "tenant_id": tenant_id,
            "version": version,
            "agent": config,
            # Generic Agent Runtime instruction (2026-09-03) — the runtime's
            # own DynamoDB permissions (authentication.tf.j2) need the real
            # table names. These are platform-wide constants (same for
            # every tenant/agent), not customer-configured deploy targets —
            # unlike PlatformSettingsRecord's AWS Target fields (tfvars.py),
            # baking them straight into the .tf file is fine, same as
            # bedrock_guardrail_name(agent_id) above.
            "dynamodb_agents_table": (
                settings.dynamodb_agents_table if settings else "panasa-agents"
            ),
            "dynamodb_versions_table": (
                settings.dynamodb_versions_table if settings else "panasa-agent-versions"
            ),
            "dynamodb_memory_table": (
                settings.dynamodb_memory_table if settings else "panasa-memory"
            ),
            "dynamodb_guardrail_policies_table": (
                settings.dynamodb_guardrail_policies_table
                if settings
                else "panasa-guardrail-policies"
            ),
            "dynamodb_knowledge_bases_table": (
                settings.dynamodb_knowledge_bases_table if settings else "panasa-knowledge-bases"
            ),
        }
        files: dict[str, str] = {}

        for module in resolved_modules:
            module_dir = self._templates_root / module
            if not module_dir.is_dir():
                continue
            for template_path in sorted(module_dir.glob("*.tf.j2")):
                relative_template = f"{module}/{template_path.name}"
                rendered = self._env.get_template(relative_template).render(**context)
                output_name = template_path.name.removesuffix(".j2")
                files[f"terraform/agents/{agent_id}/{module}__{output_name}"] = rendered

        return files
