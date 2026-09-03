"""CDK backend — renders Jinja2 .py.j2 templates into a self-contained CDK app.

Same philosophy as the Terraform backend: agent-specific values (tools,
guardrail flags, KB config, …) are baked into the generated Python source at
render time via Jinja2 — the customer's CI/CD runs `cdk synth`/`cdk deploy`
with no dependency on Panasa's AgentConfiguration class, just like it runs
`terraform apply` with no dependency on Panasa's Terraform generator.

Output layout: cdk/agents/{agent_id}/{app.py, cdk.json, requirements.txt,
stacks/{module}_stack.py}.
"""

from __future__ import annotations

from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

from app.config import Settings
from app.modules.iac_generator.backends.base import IaCBackend
from app.modules.registry.models import AgentConfiguration

_TEMPLATES_ROOT = Path(__file__).parent.parent / "templates" / "cdk"

_REQUIREMENTS_TXT = "aws-cdk-lib>=2.150.0\nconstructs>=10.0.0\n"


class CDKBackend(IaCBackend):
    tool_name = "cdk"

    def __init__(self, templates_root: Path = _TEMPLATES_ROOT) -> None:
        self._templates_root = templates_root
        self._env = Environment(
            loader=FileSystemLoader(str(templates_root)),
            autoescape=select_autoescape(disabled_extensions=("j2",), default=False),
            trim_blocks=True,
            lstrip_blocks=True,
            keep_trailing_newline=True,
        )

    def render(
        self,
        agent_id: str,
        tenant_id: str,
        version: int,
        config: AgentConfiguration,
        resolved_modules: list[str],
        settings: Settings | None = None,
    ) -> dict[str, str]:
        # tenant_id/settings: not yet consumed here — see IaCBackend.render's docstring.
        del settings
        context = {"agent_id": agent_id, "version": version, "agent": config}
        files: dict[str, str] = {}

        stack_modules = [
            module
            for module in resolved_modules
            if (self._templates_root / module / "stack.py.j2").exists()
        ]

        for module in stack_modules:
            rendered = self._env.get_template(f"{module}/stack.py.j2").render(**context)
            files[f"cdk/agents/{agent_id}/stacks/{module}_stack.py"] = rendered

        files[f"cdk/agents/{agent_id}/stacks/__init__.py"] = ""
        files[f"cdk/agents/{agent_id}/app.py"] = self._env.get_template("app.py.j2").render(
            agent_id=agent_id, version=version, modules=stack_modules
        )
        files[f"cdk/agents/{agent_id}/cdk.json"] = self._env.get_template("cdk.json.j2").render(
            agent_id=agent_id
        )
        files[f"cdk/agents/{agent_id}/requirements.txt"] = _REQUIREMENTS_TXT

        return files
