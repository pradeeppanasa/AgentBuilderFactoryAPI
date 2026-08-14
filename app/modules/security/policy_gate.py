"""Deterministic block/pass logic (CLAUDE.md Section 7 / F9 R06-R07).

No ML, no risk score that can override this. A single CRITICAL finding in
CRITICAL_BLOCK_CONDITIONS always blocks, regardless of anything else —
matching Section 7's `policy_gate()` exactly. Pure function, no I/O: the
caller (the POLICY_CHECK stage's Lambda) is responsible for persisting the
decision (see app.modules.security.policy_enforcement).
"""

from __future__ import annotations

from app.modules.change_impact.rules import CRITICAL_BLOCK_CONDITIONS
from app.modules.security.models import PolicyGateResult, SecurityFinding


def policy_gate(findings: list[SecurityFinding]) -> PolicyGateResult:
    for finding in findings:
        if finding.severity == "CRITICAL" and finding.category in CRITICAL_BLOCK_CONDITIONS:
            return PolicyGateResult(
                decision="BLOCK",
                reason=f"Critical finding: {finding.description}",
                blocking_finding=finding,
            )
    return PolicyGateResult(decision="PASS", reason="All security gates passed")
