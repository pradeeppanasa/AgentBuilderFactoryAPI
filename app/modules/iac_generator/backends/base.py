"""Pluggable IaC backend interface.

Mirrors the existing GitProvider abstraction (Section 10): one interface,
swappable implementations, selected by a single settings flag (IAC_TOOL —
same pattern as GIT_PROVIDER). Terraform and CDK both implement this;
`resolve_required_modules` (conditional.py) stays identical for both — only
rendering differs.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from app.config import Settings
from app.modules.registry.models import AgentConfiguration


class IaCBackend(ABC):
    tool_name: str

    @abstractmethod
    def render(
        self,
        agent_id: str,
        tenant_id: str,
        version: int,
        config: AgentConfiguration,
        resolved_modules: list[str],
        settings: Settings | None = None,
    ) -> dict[str, str]:
        """Return {relative_file_path: rendered_content} for every file this
        backend produces for the given resolved modules. Paths are relative
        to the artifact root (what ends up in the zip). tenant_id is only
        consumed by the Terraform backend today (per-resource tagging, Phase
        18's IaC validation suite) — CDK templates don't use it yet.
        `settings` (Generic Agent Runtime instruction, 2026-09-03) exposes
        platform-wide constants templates need baked in (e.g. the real
        DynamoDB table names) — also Terraform-only so far; optional so
        every existing call site keeps working unchanged."""
        raise NotImplementedError
