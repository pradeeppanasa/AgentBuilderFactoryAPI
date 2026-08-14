"""IaC validation report models (CLAUDE.md Section 6 — IaC validation suite).

Deliberately mirrors the shape of app.modules.security.models.SecurityFinding
/PolicyGateResult and app.modules.deployment.models.StageResult: a flat list
of named, human-readable checks rather than a nested tree, since this report
is stored on the version record (JSON blob, same pattern as
evaluation_result) and surfaced directly in an API response.
"""

from __future__ import annotations

from pydantic import BaseModel


class CheckResult(BaseModel):
    name: str
    passed: bool
    detail: str


class IaCValidationReport(BaseModel):
    passed: bool
    checks: list[CheckResult]
    tool: str
    """"terraform" | "cdk" — which backend produced the files this report
    validated. Checks beyond basic applicability are terraform-only today;
    see IaCValidator.validate's docstring."""
    generated_at: str
