"""Change Impact Analyzer (CLAUDE.md Section 7 / Phase 5).

Compares two AgentConfiguration snapshots (via the Phase 4 diff engine) and
matches each changed/added/removed field against IMPACT_RULES. Purely
deterministic — same inputs always produce the same output, no scoring.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Any

from pydantic import BaseModel

from app.modules.change_impact.rules import IMPACT_LEVEL_ORDER, IMPACT_RULES
from app.modules.registry.diff import ConfigDiff, compute_config_diff
from app.modules.registry.models import AgentConfiguration

# rule key -> the dotted-path prefix it matches (the field itself, or any
# nested path beneath it)
_WILDCARD_PREFIXES: dict[str, str] = {
    "knowledge_base.*": "knowledge_base",
    "guardrails.*": "guardrails",
    "human_review.*": "human_review",
}

_TOOL_ITEM_PATTERN = re.compile(r"^tools\[\d+\]$")
_TOOL_ENDPOINT_PATTERN = re.compile(r"^tools\[\d+\]\.endpoint$")


class ImpactAnalysis(BaseModel):
    impact_level: str  # LOW | MEDIUM | HIGH | CRITICAL
    required_validations: list[str]
    matched_rules: list[str]


class ChangeImpactAnalyzer:
    def analyze(
        self, from_config: AgentConfiguration | None, to_config: AgentConfiguration
    ) -> ImpactAnalysis:
        return self.analyze_diff(compute_config_diff(from_config, to_config))

    def analyze_diff(self, diff: ConfigDiff) -> ImpactAnalysis:
        matched_rules = self._match_rules(diff)
        return self._summarise(matched_rules)

    def _match_rules(self, diff: ConfigDiff) -> list[str]:
        matched: list[str] = []
        for changed_entry in diff.changed:
            matched.extend(
                self._match_field(changed_entry.field, kind="changed", value=changed_entry.to_value)
            )
        for added_entry in diff.added:
            matched.extend(
                self._match_field(added_entry.field, kind="added", value=added_entry.value)
            )
        for removed_entry in diff.removed:
            matched.extend(
                self._match_field(removed_entry.field, kind="removed", value=removed_entry.value)
            )
        return _dedupe(matched)

    def _match_field(self, field: str, kind: str, value: Any) -> list[str]:
        rules: list[str] = []

        if field in IMPACT_RULES:
            rules.append(field)

        for rule_key, prefix in _WILDCARD_PREFIXES.items():
            if field != prefix and not field.startswith(f"{prefix}."):
                continue
            if field == prefix and kind in ("added", "removed") and value is None:
                # An optional block (e.g. knowledge_base, human_review) that is
                # still unset showing up as "added"/"removed" on initial-version
                # creation — not a real change to that subsystem.
                continue
            rules.append(rule_key)

        if _TOOL_ITEM_PATTERN.match(field):
            if kind == "added":
                rules.append("tools[*].add")
            elif kind == "removed":
                rules.append("tools[*].remove")
        elif kind == "changed" and _TOOL_ENDPOINT_PATTERN.match(field):
            rules.append("tools[*].endpoint")

        return rules

    def _summarise(self, matched_rules: list[str]) -> ImpactAnalysis:
        if not matched_rules:
            return ImpactAnalysis(impact_level="LOW", required_validations=[], matched_rules=[])

        levels = [IMPACT_RULES[rule][0] for rule in matched_rules]
        impact_level = max(levels, key=lambda level: IMPACT_LEVEL_ORDER[level])

        validations = _dedupe(
            validation for rule in matched_rules for validation in IMPACT_RULES[rule][1]
        )

        return ImpactAnalysis(
            impact_level=impact_level,
            required_validations=validations,
            matched_rules=matched_rules,
        )


def _dedupe(items: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    ordered: list[str] = []
    for item in items:
        if item not in seen:
            seen.add(item)
            ordered.append(item)
    return ordered
