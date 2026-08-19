"""IaC validation report models (CLAUDE.md Section 6 — IaC validation suite).

Deliberately mirrors the shape of app.modules.security.models.SecurityFinding
/PolicyGateResult and app.modules.deployment.models.StageResult: a flat list
of named, human-readable checks rather than a nested tree, since this report
is stored on the version record (JSON blob, same pattern as
evaluation_result) and surfaced directly in an API response.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel

# Development Terraform Validation Mode. "local" (default) is the only mode
# implemented in Stage 1 — 100% AWS-independent (terraform fmt +
# init -backend=false + validate only, see IaCValidator). "panasa_vpc" and
# "customer_vpc" mirror CLAUDE.md Section 35's Stage 2/3 environments but
# are placeholders here: gated to admin/developer and hidden unless
# settings.dev_validation_extended_modes_enabled is explicitly turned on,
# and never perform a real deployment or touch any AWS account — doing so
# would either require Panasa's own AWS credentials (panasa_vpc, a real but
# separately-scoped feature not built in Stage 1) or would violate F0/F2/
# R03/R04 outright (customer_vpc — Panasa Runtime must never hold or use
# customer AWS credentials, ever, not just "not yet").
TerraformValidationMode = Literal["local", "panasa_vpc", "customer_vpc"]


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
