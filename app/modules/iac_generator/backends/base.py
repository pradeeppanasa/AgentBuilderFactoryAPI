"""Pluggable IaC backend interface.

Mirrors the existing GitProvider abstraction (Section 10): one interface,
swappable implementations, selected by a single settings flag (IAC_TOOL —
same pattern as GIT_PROVIDER). Terraform and CDK both implement this;
`resolve_required_modules` (conditional.py) stays identical for both — only
rendering differs.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from app.modules.registry.models import AgentConfiguration


class IaCBackend(ABC):
    tool_name: str

    @abstractmethod
    def render(
        self,
        agent_id: str,
        version: int,
        config: AgentConfiguration,
        resolved_modules: list[str],
    ) -> dict[str, str]:
        """Return {relative_file_path: rendered_content} for every file this
        backend produces for the given resolved modules. Paths are relative
        to the artifact root (what ends up in the zip)."""
        raise NotImplementedError
