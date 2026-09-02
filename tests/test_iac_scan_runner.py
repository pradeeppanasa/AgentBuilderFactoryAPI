"""IaCScanRunner — tfsec/checkov output parsing and graceful degradation.

Uses monkeypatched subprocess.run rather than the real binaries so this
suite stays fast and portable (matches IaCValidator's own tests' approach
to "terraform CLI not installed" — see validator.py's module docstring).
Manually verified once against the real installed tfsec/checkov/terraform
binaries and a real generated agent's Terraform (see the session that
introduced this file) to confirm the actual tool invocations/output shapes
this mocks are faithful to reality.
"""

from __future__ import annotations

import json
import subprocess
from typing import Any
from unittest.mock import MagicMock

import pytest

from app.config import Settings
from app.modules.deployment.iac_scan_runner import IaCScanRunner, _infer_category
from app.modules.iac_generator.validation_models import CheckResult, IaCValidationReport


class _StubIaCValidator:
    async def validate(self, **_kwargs: Any) -> IaCValidationReport:
        return IaCValidationReport(
            passed=True,
            tool="terraform",
            generated_at="2026-01-01T00:00:00Z",
            checks=[CheckResult(name="stub_check", passed=True, detail="ok")],
        )


def _settings(**overrides: Any) -> Settings:
    defaults: dict[str, Any] = {
        "dynamodb_endpoint": "http://localhost:8001",
        "deployment_mode": "prototype",
        "tfsec_binary_path": "tfsec",
        "checkov_python_path": None,
    }
    defaults.update(overrides)
    return Settings(**defaults)


def _runner(**settings_overrides: Any) -> IaCScanRunner:
    return IaCScanRunner(_settings(**settings_overrides), _StubIaCValidator())


TFSEC_JSON = json.dumps(
    {
        "results": [
            {
                "long_id": "aws-s3-block-public-acls",
                "rule_description": "S3 Access block should block public ACL",
                "description": "No public access block so not blocking public acls",
                "severity": "HIGH",
                "resource": "aws_s3_bucket.agent_audit",
            },
            {
                "long_id": "general-secrets-sensitive-credentials",
                "rule_description": "Potential secret found in resource attribute",
                "description": "Hardcoded secret detected",
                "severity": "CRITICAL",
                "resource": "aws_lambda_function.tool",
            },
        ]
    }
)


def test_run_tfsec_parses_findings_and_infers_category(monkeypatch: pytest.MonkeyPatch) -> None:
    runner = _runner()
    fake_result = MagicMock(stdout=TFSEC_JSON, stderr="")
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: fake_result)

    findings = runner._run_tfsec(tmpdir=__import__("pathlib").Path("."))

    assert len(findings) == 2
    assert findings[0].severity == "HIGH"
    # no keyword match — passthrough of the combined rule id + description
    assert findings[0].category == (
        "aws-s3-block-public-acls S3 Access block should block public ACL"
    )
    assert findings[0].location == "aws_s3_bucket.agent_audit"
    assert findings[1].severity == "CRITICAL"
    assert findings[1].category == "hardcoded_secret_found"  # "secret" keyword inferred


def test_run_tfsec_returns_empty_when_binary_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    runner = _runner()

    def _raise(*_a: Any, **_k: Any) -> None:
        raise FileNotFoundError

    monkeypatch.setattr(subprocess, "run", _raise)

    assert runner._run_tfsec(tmpdir=__import__("pathlib").Path(".")) == []


def test_run_checkov_skips_when_not_configured() -> None:
    runner = _runner(checkov_python_path=None)

    assert runner._run_checkov(tmpdir=__import__("pathlib").Path(".")) == []


def test_run_checkov_parses_failed_checks_with_default_severity(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    # checkov's --output-file-path creates a *directory* containing
    # results_json.json, not a plain file — reproduced against the real
    # binary (see the session that introduced this file). Reading stdout
    # directly (like tfsec) sidesteps that entirely, so this mock matches
    # the real invocation: no --output-file-path flag at all.
    runner = _runner(checkov_python_path="python")
    checkov_output = {
        "results": {
            "failed_checks": [
                {
                    "check_id": "CKV_AWS_76",
                    "check_name": "Ensure API Gateway has Access Logging enabled",
                    "severity": None,
                    "resource": "aws_apigatewayv2_stage.agent_api_stage",
                }
            ]
        }
    }
    fake_result = MagicMock(stdout=json.dumps(checkov_output), stderr="")
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: fake_result)

    findings = runner._run_checkov(tmpdir=tmp_path)

    assert len(findings) == 1
    assert findings[0].severity == "MEDIUM"  # community edition gives no real severity
    assert findings[0].location == "aws_apigatewayv2_stage.agent_api_stage"


@pytest.mark.parametrize(
    ("rule_text", "expected"),
    [
        ("aws-iam-no-policy-wildcards", "iam_privilege_escalation"),
        ("general-secrets-sensitive-credentials", "hardcoded_secret_found"),
        ("aws-api-gateway-enable-access-logging", "aws-api-gateway-enable-access-logging"),
    ],
)
def test_infer_category(rule_text: str, expected: str) -> None:
    assert _infer_category(rule_text) == expected
