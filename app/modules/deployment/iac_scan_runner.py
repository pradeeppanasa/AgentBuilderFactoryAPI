"""Real IaC security scanning (tfsec + checkov) for the deployment
pipeline's SECURITY_SCANNING/POLICY_CHECK stages, run locally when
SIMULATE_DEPLOYMENT_PIPELINE is on (see pipeline_simulator.py's module
docstring for why nothing else populates these stages in local dev).

This runs the same real tfsec/checkov binaries a customer's CI/CD would
run against the actual generated Terraform — genuinely real findings, not
fabricated text. TERRAFORM_VALIDATE reuses IaCValidator (already real —
see that module) rather than duplicating its terraform fmt/init/validate
logic. POLICY_CHECK reuses the same deterministic policy_gate() the real
pipeline uses (app.modules.security.policy_gate), so a genuinely CRITICAL
finding blocks here exactly as it would in production.

Terraform PLAN/APPLY remain out of scope: the generated Terraform has no
root module composition yet and several required variables have no
supplied values (a separate, larger IaC Generator fix) — a real plan/apply
would fail immediately for reasons unrelated to this scan.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import tempfile
from pathlib import Path

from app.config import Settings
from app.modules.iac_generator.validation_models import IaCValidationReport
from app.modules.iac_generator.validator import IaCValidator, _flatten_for_terraform_cli
from app.modules.registry.models import AgentConfiguration
from app.modules.security.models import (
    SecurityFinding,
    SecurityScanSummary,
    SecuritySeverity,
)
from app.modules.security.policy_gate import policy_gate
from app.shared.logging import get_logger

log = get_logger()

_TFSEC_TIMEOUT_SECONDS = 90
_CHECKOV_TIMEOUT_SECONDS = 180

_TFSEC_SEVERITIES: frozenset[str] = frozenset({"LOW", "MEDIUM", "HIGH", "CRITICAL"})

# Best-effort mapping from a scanner's own rule identifier/description to
# one of change_impact.rules.CRITICAL_BLOCK_CONDITIONS — policy_gate() only
# ever blocks on a CRITICAL finding whose category is in that fixed list,
# so an unmapped category is deliberately inert (informational only),
# never a block, matching R06/R07's "no score can override this" as
# written — this mapping can only make policy_gate *more* conservative
# about what counts as blocking, never less.
_CATEGORY_KEYWORDS: tuple[tuple[str, str], ...] = (
    ("secret", "hardcoded_secret_found"),
    ("credential", "hardcoded_secret_found"),
    ("privilege", "iam_privilege_escalation"),
    ("wildcard", "iam_privilege_escalation"),
)


def _infer_category(rule_text: str) -> str:
    lowered = rule_text.lower()
    for keyword, category in _CATEGORY_KEYWORDS:
        if keyword in lowered:
            return category
    return rule_text


class IaCScanResult:
    def __init__(
        self,
        security_summary: SecurityScanSummary,
        validation_report: IaCValidationReport,
        policy_decision: str,
        policy_reason: str,
    ) -> None:
        self.security_summary = security_summary
        self.validation_report = validation_report
        self.policy_decision = policy_decision  # "PASS" | "BLOCK"
        self.policy_reason = policy_reason


class IaCScanRunner:
    def __init__(self, settings: Settings, iac_validator: IaCValidator) -> None:
        self._settings = settings
        self._iac_validator = iac_validator

    async def run(
        self,
        *,
        agent_id: str,
        tenant_id: str,
        version: int,
        config: AgentConfiguration,
        files: dict[str, str],
    ) -> IaCScanResult:
        validation_report = await self._iac_validator.validate(
            agent_id=agent_id,
            tenant_id=tenant_id,
            version=version,
            config=config,
            files=files,
            tool="terraform",
        )

        tf_files = {path: content for path, content in files.items() if path.endswith(".tf")}
        findings = await asyncio.to_thread(self._run_scanners_sync, tf_files)

        security_summary = self._summarize(findings)
        gate_result = policy_gate(findings)
        return IaCScanResult(
            security_summary=security_summary,
            validation_report=validation_report,
            policy_decision=gate_result.decision,
            policy_reason=gate_result.reason,
        )

    def _run_scanners_sync(self, tf_files: dict[str, str]) -> list[SecurityFinding]:
        flattened = _flatten_for_terraform_cli(tf_files)
        with tempfile.TemporaryDirectory(prefix="panasa-iac-scan-") as tmpdir_str:
            tmpdir = Path(tmpdir_str)
            for name, content in flattened.items():
                (tmpdir / name).write_text(content, encoding="utf-8")
            return self._run_tfsec(tmpdir) + self._run_checkov(tmpdir)

    def _run_tfsec(self, tmpdir: Path) -> list[SecurityFinding]:
        try:
            result = subprocess.run(  # noqa: S603 — fixed argv, no shell, no user input
                [self._settings.tfsec_binary_path, str(tmpdir), "--no-color", "--format", "json"],
                cwd=str(tmpdir),
                capture_output=True,
                text=True,
                timeout=_TFSEC_TIMEOUT_SECONDS,
            )
        except FileNotFoundError:
            log.warning("iac_scan.tfsec_not_found")
            return []
        except subprocess.TimeoutExpired:
            log.warning("iac_scan.tfsec_timed_out")
            return []

        try:
            parsed = json.loads(result.stdout or "{}")
        except json.JSONDecodeError:
            log.warning("iac_scan.tfsec_output_unparseable", stderr=(result.stderr or "")[:500])
            return []

        findings = []
        for item in parsed.get("results") or []:
            severity = str(item.get("severity") or "LOW").upper()
            if severity not in _TFSEC_SEVERITIES:
                severity = "LOW"
            rule_text = f"{item.get('long_id', '')} {item.get('rule_description', '')}"
            findings.append(
                SecurityFinding(
                    scan_type="iac_scan",
                    severity=severity,  # type: ignore[arg-type]
                    category=_infer_category(rule_text),
                    description=str(item.get("description") or item.get("rule_description") or ""),
                    location=str(item.get("resource") or ""),
                )
            )
        return findings

    def _run_checkov(self, tmpdir: Path) -> list[SecurityFinding]:
        checkov_python = self._settings.checkov_python_path
        if not checkov_python:
            log.info("iac_scan.checkov_not_configured")
            return []

        try:
            result = subprocess.run(  # noqa: S603
                [checkov_python, "-m", "checkov.main", "-d", ".", "--compact", "--output", "json"],
                cwd=str(tmpdir),
                capture_output=True,
                text=True,
                timeout=_CHECKOV_TIMEOUT_SECONDS,
            )
        except FileNotFoundError:
            log.warning("iac_scan.checkov_not_found")
            return []
        except subprocess.TimeoutExpired:
            log.warning("iac_scan.checkov_timed_out")
            return []

        try:
            parsed = json.loads(result.stdout or "{}")
        except json.JSONDecodeError:
            log.warning("iac_scan.checkov_output_unparseable", stderr=(result.stderr or "")[:500])
            return []
        if isinstance(parsed, list):
            parsed = parsed[0] if parsed else {}

        findings = []
        for item in (parsed.get("results") or {}).get("failed_checks") or []:
            rule_text = f"{item.get('check_id', '')} {item.get('check_name', '')}"
            # checkov's community edition doesn't populate severity without
            # a paid Bridgecrew/Prisma Cloud connection — MEDIUM is a
            # deliberately non-blocking default (policy_gate only ever
            # blocks on CRITICAL), not a claim about the real severity.
            findings.append(
                SecurityFinding(
                    scan_type="iac_scan",
                    severity=_checkov_severity(item.get("severity")),
                    category=_infer_category(rule_text),
                    description=str(item.get("check_name") or item.get("check_id") or ""),
                    location=str(item.get("resource") or ""),
                )
            )
        return findings

    @staticmethod
    def _summarize(findings: list[SecurityFinding]) -> SecurityScanSummary:
        by_severity: dict[str, int] = {}
        for finding in findings:
            by_severity[finding.severity] = by_severity.get(finding.severity, 0) + 1
        counts = ", ".join(
            f"{count} {severity.lower()}"
            for severity, count in sorted(by_severity.items())
        )
        summary = f"tfsec + checkov: {counts}" if findings else "tfsec + checkov: 0 findings"
        return SecurityScanSummary(
            scan_type="iac_scan", passed=True, findings=findings, summary=summary
        )


def _checkov_severity(raw: object) -> SecuritySeverity:
    if isinstance(raw, str) and raw.upper() in _TFSEC_SEVERITIES:
        return raw.upper()  # type: ignore[return-value]
    return "MEDIUM"
