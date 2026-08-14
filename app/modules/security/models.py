"""Security scanning models (CLAUDE.md Section 6.2 / Section 7 / Phase 9).

Findings carry a `location` (file:line, resource address) but never raw
tool output or payloads — Section 4.4's StageResult.output_summary rule
("human-readable, never raw secrets or payloads") applies here too.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

SecurityScanType = Literal["sast", "secret_scan", "dependency_scan", "iac_scan", "container_scan"]

SCAN_TYPES: tuple[SecurityScanType, ...] = (
    "sast",
    "secret_scan",
    "dependency_scan",
    "iac_scan",
    "container_scan",
)

# Which underlying tool each scan type runs (Section 6.2).
SCAN_TOOLS: dict[SecurityScanType, str] = {
    "sast": "Bandit + Semgrep",
    "secret_scan": "Trufflehog",
    "dependency_scan": "Safety",
    "iac_scan": "Checkov + tfsec",
    "container_scan": "Trivy",
}

SecuritySeverity = Literal["LOW", "MEDIUM", "HIGH", "CRITICAL"]


class SecurityFinding(BaseModel):
    scan_type: SecurityScanType
    severity: SecuritySeverity
    category: str
    """Free-form for most findings; must be one of CRITICAL_BLOCK_CONDITIONS
    (app.modules.change_impact.rules) when severity == "CRITICAL" for
    policy_gate to block on it."""
    description: str
    location: str | None = None  # e.g. "app/main.py:42" or a TF resource address


class SecurityScanSummary(BaseModel):
    scan_type: SecurityScanType
    passed: bool
    findings: list[SecurityFinding] = Field(default_factory=list)
    summary: str  # e.g. "SAST: 0 critical, 2 medium"


class PolicyGateResult(BaseModel):
    decision: Literal["PASS", "BLOCK"]
    reason: str
    blocking_finding: SecurityFinding | None = None
