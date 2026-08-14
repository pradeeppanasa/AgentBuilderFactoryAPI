"""MarkBlocked (CLAUDE.md Section 6.2) — terminal state reached only via
PolicyGateChoice's BLOCK branch. Idempotent no-op: policy_check.py already
persisted BLOCKED status and closed the PR as part of its own tested
enforce_policy_gate() contract (Phase 9). This state exists so Step
Functions' own execution history names the terminal outcome "MarkBlocked"
for operators, not to do a second write.
"""

from __future__ import annotations

from typing import Any


def handler(event: dict[str, Any], _context: Any) -> dict[str, Any]:
    return {
        "status": "BLOCKED",
        "reason": event.get("policyCheck", {}).get("reason", "Blocked by policy gate"),
    }
