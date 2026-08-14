"""Feature resolver (CLAUDE.md Section 8 / R20).

R20: IaC generation is conditional — only generate resources required by the
agent's actual config. This is backend-agnostic: the same resolved module
list drives Terraform generation or CDK generation identically. Only the
rendering differs.
"""

from __future__ import annotations

from app.modules.registry.models import AgentConfiguration

_ALWAYS_ON_MODULES = ["base", "api_gateway", "authentication", "orchestrator", "observability"]


def resolve_required_modules(config: AgentConfiguration) -> list[str]:
    """Return the list of IaC module names required by this configuration."""
    modules = list(_ALWAYS_ON_MODULES)

    if config.guardrails and any(
        [
            config.guardrails.prompt_injection,
            config.guardrails.pii_detection,
            config.guardrails.toxicity_filter,
        ]
    ):
        modules.append("guardrails")

    if config.knowledge_base and config.knowledge_base.enabled:
        modules.append("rag")

    if config.tools:
        modules.append("tools")

    if config.human_review and config.human_review.enabled:
        modules.append("human_loop")

    if config.audit_enabled:
        modules.append("audit")

    return modules
