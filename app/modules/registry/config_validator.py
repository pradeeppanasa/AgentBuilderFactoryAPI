"""Agent Supply-Chain Security — config validation at create/update time
(Sprint 3 Phase 7 — CLAUDE.md Section 62.3, S-13a).

Adapted from the CLAUDE.md skeleton in one respect: check #5 there
("system prompt scanned by [an] ONNX BERT classifier") assumes a
classifier this Factory Runtime doesn't have — that model lives inside
the completely separate Generated Agent Runtime's own guardrail.py
(Section 51.4), a different service with zero shared code (F8). Wiring a
live Bedrock Guardrails call (this Runtime's own GuardrailEngine,
app/modules/guardrails/engine.py) into a synchronous create/update
request path would make agent creation depend on Bedrock availability
for every agent, including the many agents in this codebase's own tests
and real use that have no guardrail_policy_id configured at all — a real
behavioural risk for something that isn't one of Phase 7's own
acceptance criteria. Not implemented here; tracked as its own item
(CLAUDE.md Section 65) rather than silently built as something it isn't.
"""

from __future__ import annotations

from typing import Any

from app.modules.registry.models import AgentConfiguration
from app.modules.tool_registry.store import ToolRegistryStore

_WILDCARD_SCOPES = {"*", "all", "admin"}

_CREDENTIAL_KEY_MARKERS = ("password", "secret", "token", "api_key", "credential")
"""Matched against dict keys, not values — a field ending in `_arn` is
always exempt (R55's own "reference ONLY, never the value" convention;
`credentials_secret_arn` is the correct, expected way to reference a
credential and must never trip this check)."""


def _looks_like_embedded_credential(key: str, value: str) -> bool:
    if not value:
        return False
    lowered_key = key.lower()
    if lowered_key.endswith("_arn") or lowered_key == "arn":
        return False
    return any(marker in lowered_key for marker in _CREDENTIAL_KEY_MARKERS)


def _find_embedded_credentials(obj: Any, path: str = "") -> list[str]:
    """Recursively walks a config's dict form looking for a value sitting
    under a credential-shaped key name. Returns human-readable paths, not
    the values themselves — an error message must never repeat back the
    very credential it's warning about."""
    findings: list[str] = []
    if isinstance(obj, dict):
        for key, value in obj.items():
            field_path = f"{path}.{key}" if path else str(key)
            if isinstance(value, str) and _looks_like_embedded_credential(key, value):
                findings.append(field_path)
            else:
                findings.extend(_find_embedded_credentials(value, field_path))
    elif isinstance(obj, list):
        for index, item in enumerate(obj):
            findings.extend(_find_embedded_credentials(item, f"{path}[{index}]"))
    return findings


class AgentConfigValidator:
    def __init__(self, tool_registry_store: ToolRegistryStore) -> None:
        self._tool_registry_store = tool_registry_store

    async def validate(self, config: AgentConfiguration, tenant_id: str) -> list[str]:
        """tenant_id is accepted (matching the CLAUDE.md instruction's own
        call signature) but not yet used by any check below — the tool
        registry (Section 62.2) is a flat, platform-wide catalog, not
        tenant-scoped (ToolRegistryStore's own docstring). Kept as a
        forward-compatible extension point rather than dropped, since a
        future tenant-specific custom tool registry is a plausible need
        this signature already accounts for.
        """
        errors: list[str] = []

        # 1. Every configured tool must be an APPROVED entry in the
        # registry (Section 62.2) — a Lambda existing for a tool_id is
        # not the same as that tool having been reviewed at all.
        for tool in config.tools:
            entry = await self._tool_registry_store.get(tool.tool_id)
            if entry is None or entry.status != "APPROVED":
                errors.append(f"Tool {tool.tool_id!r} is not in the approved tool registry")

        # 2. Every configured tool must have a tool_policies entry
        # (Phase 4's ToolPolicyEngine denies by default otherwise — this
        # check surfaces that at config-save time rather than letting an
        # admin discover it only when the agent silently can't use its
        # own tool).
        policy_tool_ids = {policy.tool for policy in config.tool_policies}
        for tool in config.tools:
            if tool.tool_id not in policy_tool_ids:
                errors.append(f"Tool {tool.tool_id!r} has no policy entry — add to tool_policies")

        # 3. No wildcard scope.
        for policy in config.tool_policies:
            if policy.scope is not None and policy.scope.lower() in _WILDCARD_SCOPES:
                errors.append(f"Wildcard scope not allowed on tool {policy.tool!r}")

        # 4. No secrets or credentials embedded directly in config
        # fields — R55 requires a Secrets Manager ARN reference instead.
        for field_path in _find_embedded_credentials(config.model_dump()):
            errors.append(
                f"Config field {field_path!r} looks like it contains credential "
                "data. Use a Secrets Manager ARN reference instead."
            )

        return errors
