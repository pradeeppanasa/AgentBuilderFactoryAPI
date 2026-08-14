"""Unit tests for policy_gate (CLAUDE.md Section 7) — one per CRITICAL_BLOCK_CONDITIONS entry."""

from app.modules.change_impact.rules import CRITICAL_BLOCK_CONDITIONS
from app.modules.security.models import SecurityFinding
from app.modules.security.policy_gate import policy_gate


def _finding(severity: str, category: str, description: str = "issue") -> SecurityFinding:
    return SecurityFinding(
        scan_type="sast",
        severity=severity,  # type: ignore[arg-type]
        category=category,
        description=description,
    )


def test_no_findings_passes() -> None:
    result = policy_gate([])
    assert result.decision == "PASS"
    assert result.blocking_finding is None


def test_non_critical_findings_pass() -> None:
    findings = [
        _finding("LOW", "hardcoded_secret_found"),
        _finding("MEDIUM", "critical_cve_found"),
        _finding("HIGH", "iam_privilege_escalation"),
    ]
    result = policy_gate(findings)
    assert result.decision == "PASS"


def test_critical_finding_with_unlisted_category_passes() -> None:
    # CRITICAL severity alone isn't enough — category must be a listed condition.
    result = policy_gate([_finding("CRITICAL", "some_other_category")])
    assert result.decision == "PASS"


def test_critical_block_conditions_table_matches_spec() -> None:
    assert CRITICAL_BLOCK_CONDITIONS == [
        "hardcoded_secret_found",
        "critical_cve_found",
        "iam_privilege_escalation",
        "prompt_injection_vulnerability",
        "data_exfiltration_risk",
    ]


def test_hardcoded_secret_found_blocks() -> None:
    finding = _finding("CRITICAL", "hardcoded_secret_found", "AWS key in app/config.py")
    result = policy_gate([finding])
    assert result.decision == "BLOCK"
    assert result.blocking_finding == finding
    assert "AWS key in app/config.py" in result.reason


def test_critical_cve_found_blocks() -> None:
    result = policy_gate([_finding("CRITICAL", "critical_cve_found")])
    assert result.decision == "BLOCK"


def test_iam_privilege_escalation_blocks() -> None:
    result = policy_gate([_finding("CRITICAL", "iam_privilege_escalation")])
    assert result.decision == "BLOCK"


def test_prompt_injection_vulnerability_blocks() -> None:
    result = policy_gate([_finding("CRITICAL", "prompt_injection_vulnerability")])
    assert result.decision == "BLOCK"


def test_data_exfiltration_risk_blocks() -> None:
    result = policy_gate([_finding("CRITICAL", "data_exfiltration_risk")])
    assert result.decision == "BLOCK"


def test_one_blocking_finding_among_many_non_blocking_still_blocks() -> None:
    findings = [
        _finding("LOW", "hardcoded_secret_found"),
        _finding("HIGH", "some_other_category"),
        _finding("CRITICAL", "iam_privilege_escalation", "wildcard IAM policy"),
        _finding("MEDIUM", "critical_cve_found"),
    ]
    result = policy_gate(findings)
    assert result.decision == "BLOCK"
    assert result.blocking_finding is not None
    assert result.blocking_finding.category == "iam_privilege_escalation"
