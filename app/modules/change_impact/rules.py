"""Deterministic change-impact rules table (CLAUDE.md Section 7).

No ML, no risk score that can override a rule — a matched rule always
contributes its full required_validations list, and the overall impact
level is always the highest level of any rule that matched. HIGH/CRITICAL
here is informational only (F1/R08): it does not gate a deployment by
itself — that's the (separate, later) Policy Gate's job.

Five keys — lambda_code, iam_policy, network_config, container_image,
runtime_upgrade — describe infrastructure-level changes with no
corresponding AgentConfiguration field. They're kept here because Section 7
defines them as part of the one canonical table, but nothing in this phase
can match them: there is no AgentConfiguration diff that produces a
"lambda_code changed" entry. They become reachable once IaC-level diffing
(Terraform plan comparison) exists in a later phase.
"""

from __future__ import annotations

# field/pattern -> (impact_level, required_validations)
IMPACT_RULES: dict[str, tuple[str, list[str]]] = {
    "system_prompt": ("MEDIUM", ["PROMPT_EVALUATION", "GUARDRAIL_TESTS"]),
    "model_id": ("HIGH", ["MODEL_EVALUATION", "SAFETY_TESTS"]),
    "model_provider": ("HIGH", ["MODEL_EVALUATION", "SAFETY_TESTS", "FULL_SECURITY"]),
    "temperature": ("LOW", ["PROMPT_EVALUATION"]),
    "max_tokens": ("LOW", ["PROMPT_EVALUATION"]),
    "knowledge_base.*": ("MEDIUM", ["RAG_EVALUATION"]),
    "guardrails.*": ("HIGH", ["FULL_GUARDRAIL_REGRESSION"]),
    "tools[*].add": ("HIGH", ["TOOL_SECURITY", "INTEGRATION_TESTS", "IAC_SCAN"]),
    "tools[*].remove": ("MEDIUM", ["INTEGRATION_TESTS"]),
    "tools[*].endpoint": ("HIGH", ["TOOL_SECURITY", "INTEGRATION_TESTS"]),
    "human_review.*": ("MEDIUM", ["INTEGRATION_TESTS"]),
    "token_budget_daily": ("LOW", []),
    "rate_limit_rpm": ("LOW", []),
    "lambda_code": ("HIGH", ["SAST", "DEPENDENCY_SCAN", "INTEGRATION_TESTS"]),
    "iam_policy": ("HIGH", ["IAC_SCAN", "POLICY_VALIDATION"]),
    "network_config": ("HIGH", ["IAC_SCAN", "SECURITY_SCAN"]),
    "container_image": ("CRITICAL", ["FULL_SECURITY", "CONTAINER_SCAN", "FULL_REGRESSION"]),
    "runtime_upgrade": ("CRITICAL", ["FULL_SECURITY", "FULL_REGRESSION"]),
}

IMPACT_LEVEL_ORDER: dict[str, int] = {"LOW": 0, "MEDIUM": 1, "HIGH": 2, "CRITICAL": 3}

# Critical findings always block regardless of risk score (consumed by the
# Policy Gate — a later phase, once SecurityFinding results exist).
CRITICAL_BLOCK_CONDITIONS: list[str] = [
    "hardcoded_secret_found",
    "critical_cve_found",
    "iam_privilege_escalation",
    "prompt_injection_vulnerability",
    "data_exfiltration_risk",
]
