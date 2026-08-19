"""Terraform backend — renders Jinja2 .tf.j2 templates per resolved module.

Output layout: terraform/agents/{agent_id}/{module}/{file}.tf — matches the
naming convention already used elsewhere for generated per-agent Terraform
(e.g. F6's network.tf, Section 19's schedule_{schedule_id}.tf).
"""

from __future__ import annotations

from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

from app.modules.iac_generator.backends.base import IaCBackend
from app.modules.iac_generator.naming import bedrock_guardrail_name
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

    def render(
        self,
        agent_id: str,
        tenant_id: str,
        version: int,
        config: AgentConfiguration,
        resolved_modules: list[str],
    ) -> dict[str, str]:
        context = {
            "agent_id": agent_id,
            "tenant_id": tenant_id,
            "version": version,
            "agent": config,
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
                files[f"terraform/agents/{agent_id}/{module}/{output_name}"] = rendered

        return files
