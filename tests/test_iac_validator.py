"""Tests for app.modules.iac_generator.validator (CLAUDE.md Section 6 — IaC
validation suite).

Two kinds of coverage:
  - One config per conditional module (KB on/off, tools on/off, human_review
    on/off), rendered through the REAL TerraformBackend and checked with the
    REAL IaCValidator — end-to-end, not mocked, matching what
    POST /agents/{id}/generate-iac actually runs.
  - Synthetic hand-written HCL exercising each check's failure path
    directly, so a check that always silently passes would be caught here
    (a validator with no way to fail is not a validator).

terraform CLI is not installed in this environment (verified: `terraform
version` -> command not found), so terraform_fmt/terraform_validate are
expected to report passed=True with a "Skipped" detail throughout — that is
itself the behaviour under test in test_terraform_cli_checks_skip_gracefully.
"""

from __future__ import annotations

from typing import Any

from app.modules.iac_generator.backends.terraform import TerraformBackend
from app.modules.iac_generator.conditional import resolve_required_modules
from app.modules.iac_generator.validator import IaCValidator, _parse_terraform_validate_diagnostics
from app.modules.registry.models import AgentConfiguration

_AGENT_ID = "kyc-agent-001"
_TENANT_ID = "tenant-a"
_VERSION = 3

_backend = TerraformBackend()
_validator = IaCValidator()


def _config(**overrides: Any) -> AgentConfiguration:
    data: dict[str, Any] = {
        "model_id": "anthropic.claude-3-5-sonnet-20241022-v2:0",
        "model_provider": "bedrock",
        "system_prompt": "You are a KYC verification agent.",
    }
    data.update(overrides)
    return AgentConfiguration(**data)


def _checks_by_name(report: Any) -> dict[str, Any]:
    return {c.name: c for c in report.checks}


async def _render_and_validate(config: AgentConfiguration) -> Any:
    modules = resolve_required_modules(config)
    files = _backend.render(_AGENT_ID, _TENANT_ID, _VERSION, config, modules)
    return await _validator.validate(
        agent_id=_AGENT_ID,
        tenant_id=_TENANT_ID,
        version=_VERSION,
        config=config,
        files=files,
        tool="terraform",
    )


# ── One config per conditional module — resource presence/absence ───────


async def test_kb_enabled_contains_opensearch_serverless_collection() -> None:
    config = _config(
        knowledge_base={"enabled": True, "kb_name": "kyc-docs", "s3_bucket": "kyc-bucket"}
    )
    report = await _render_and_validate(config)

    checks = _checks_by_name(report)
    assert checks["resource_presence"].passed, checks["resource_presence"].detail
    assert report.passed, [c for c in report.checks if not c.passed]


async def test_kb_disabled_contains_no_opensearch_resources() -> None:
    config = _config(knowledge_base={"enabled": False})
    modules = resolve_required_modules(config)
    files = _backend.render(_AGENT_ID, _TENANT_ID, _VERSION, config, modules)

    assert not any("opensearch" in content.lower() for content in files.values())

    report = await _validator.validate(
        agent_id=_AGENT_ID,
        tenant_id=_TENANT_ID,
        version=_VERSION,
        config=config,
        files=files,
        tool="terraform",
    )
    assert _checks_by_name(report)["resource_presence"].passed
    assert report.passed


async def test_tools_configured_contains_one_lambda_per_tool() -> None:
    config = _config(
        tools=[
            {
                "tool_id": "jira",
                "tool_name": "Jira",
                "executor_type": "http",
                "endpoint": "https://acme.atlassian.net",
            },
            {
                "tool_id": "salesforce",
                "tool_name": "Salesforce",
                "executor_type": "http",
                "endpoint": "https://acme.my.salesforce.com",
            },
        ]
    )
    modules = resolve_required_modules(config)
    files = _backend.render(_AGENT_ID, _TENANT_ID, _VERSION, config, modules)
    joined = "\n".join(files.values())
    assert joined.count('resource "aws_lambda_function"') == 2

    report = await _validator.validate(
        agent_id=_AGENT_ID,
        tenant_id=_TENANT_ID,
        version=_VERSION,
        config=config,
        files=files,
        tool="terraform",
    )
    assert _checks_by_name(report)["resource_presence"].passed
    assert report.passed, [c for c in report.checks if not c.passed]


async def test_no_tools_contains_no_lambda_function() -> None:
    config = _config(tools=[])
    report = await _render_and_validate(config)

    assert _checks_by_name(report)["resource_presence"].passed
    assert report.passed


async def test_human_review_enabled_contains_sqs_queue() -> None:
    config = _config(human_review={"enabled": True, "trigger_conditions": ["high_risk_decision"]})
    modules = resolve_required_modules(config)
    files = _backend.render(_AGENT_ID, _TENANT_ID, _VERSION, config, modules)
    assert any("aws_sqs_queue" in content for content in files.values())

    report = await _validator.validate(
        agent_id=_AGENT_ID,
        tenant_id=_TENANT_ID,
        version=_VERSION,
        config=config,
        files=files,
        tool="terraform",
    )
    assert _checks_by_name(report)["resource_presence"].passed
    assert report.passed, [c for c in report.checks if not c.passed]


async def test_human_review_disabled_contains_no_sqs_queue() -> None:
    config = _config(human_review={"enabled": False})
    modules = resolve_required_modules(config)
    files = _backend.render(_AGENT_ID, _TENANT_ID, _VERSION, config, modules)
    assert not any("aws_sqs_queue" in content for content in files.values())

    report = await _validator.validate(
        agent_id=_AGENT_ID,
        tenant_id=_TENANT_ID,
        version=_VERSION,
        config=config,
        files=files,
        tool="terraform",
    )
    assert _checks_by_name(report)["resource_presence"].passed
    assert report.passed


async def test_fully_featured_agent_passes_every_structural_check() -> None:
    """All three conditional modules on at once — the realistic KYC-agent
    shape from the Phase 17 e2e scenario."""
    config = _config(
        guardrails={
            "prompt_injection": True,
            "pii_detection": True,
            "toxicity_filter": True,
            "hallucination_check": True,
            "pii_strip_output": True,
        },
        knowledge_base={"enabled": True, "kb_name": "kyc-docs", "s3_bucket": "kyc-bucket"},
        tools=[
            {
                "tool_id": "jira",
                "tool_name": "Jira",
                "executor_type": "http",
                "endpoint": "https://acme.atlassian.net",
                "credentials_secret_arn": (
                    "arn:aws:secretsmanager:eu-west-2:123456789012:secret:jira"
                ),
            }
        ],
        human_review={"enabled": True, "trigger_conditions": ["high_risk_decision"]},
    )
    report = await _render_and_validate(config)

    failed = [c for c in report.checks if not c.passed]
    assert not failed, failed
    assert report.passed


async def test_minimal_agent_passes_every_structural_check() -> None:
    """No KB, no tools, no human review — the other extreme."""
    report = await _render_and_validate(_config())

    failed = [c for c in report.checks if not c.passed]
    assert not failed, failed
    assert report.passed


# ── Backend applicability ────────────────────────────────────────────────


async def test_cdk_backend_is_not_checked_structurally() -> None:
    report = await _validator.validate(
        agent_id=_AGENT_ID,
        tenant_id=_TENANT_ID,
        version=_VERSION,
        config=_config(),
        files={"cdk/agents/kyc-agent-001/app.py": "# not terraform"},
        tool="cdk",
    )
    assert report.passed is True
    assert report.tool == "cdk"
    assert len(report.checks) == 1
    assert report.checks[0].name == "terraform_only"


async def test_terraform_cli_checks_skip_gracefully_when_not_installed() -> None:
    report = await _render_and_validate(_config())
    checks = _checks_by_name(report)

    assert checks["terraform_fmt"].passed is True
    assert "skipped" in checks["terraform_fmt"].detail.lower()
    assert checks["terraform_validate"].passed is True
    assert "skipped" in checks["terraform_validate"].detail.lower()


# ── Synthetic HCL — each check's failure path ────────────────────────────


async def _validate_raw(hcl: str, config: AgentConfiguration | None = None) -> Any:
    return await _validator.validate(
        agent_id="agent-x",
        tenant_id=_TENANT_ID,
        version=1,
        config=config or _config(),
        files={"terraform/agents/agent-x/synthetic/synthetic.tf": hcl},
        tool="terraform",
    )


async def test_naming_convention_catches_unprefixed_resource() -> None:
    report = await _validate_raw(
        """
        resource "aws_s3_bucket" "bad" {
          bucket = "my-bucket-with-no-prefix"
        }
        """
    )
    check = _checks_by_name(report)["naming_convention"]
    assert not check.passed
    assert "my-bucket-with-no-prefix" in check.detail
    assert not report.passed


async def test_naming_convention_accepts_truncated_guardrail_name_for_long_agent_id() -> None:
    """QA I-02 — a real agent_id long enough that panasa-{agent_id}-guardrail
    exceeds AWS Bedrock's 50-char guardrail name limit (e.g. a slugified
    long agent name + random suffix) must still pass naming_convention
    against the truncated name the template actually renders, not be
    flagged as a violation for not using the literal full agent_id."""
    long_agent_id = "kyc-document-verification-agent-3181e1"  # 39 chars
    assert len(f"panasa-{long_agent_id}-guardrail") > 50  # sanity: this is the real overflow case

    config = _config()
    modules = resolve_required_modules(config)
    files = _backend.render(long_agent_id, _TENANT_ID, _VERSION, config, modules)
    report = await _validator.validate(
        agent_id=long_agent_id,
        tenant_id=_TENANT_ID,
        version=_VERSION,
        config=config,
        files=files,
        tool="terraform",
    )

    naming_check = _checks_by_name(report)["naming_convention"]
    assert naming_check.passed, naming_check.detail

    guardrail_tf = files[f"terraform/agents/{long_agent_id}/guardrails__guardrails.tf"]
    assert 'name                      = "panasa-' in guardrail_tf
    # The rendered name itself must respect the real AWS limit.
    for line in guardrail_tf.splitlines():
        if line.strip().startswith("name") and "guardrail" in line:
            rendered_name = line.split("=", 1)[1].strip().strip('"')
            assert len(rendered_name) <= 50
            break
    else:
        raise AssertionError("guardrail name line not found in rendered template")


def test_parse_terraform_validate_diagnostics_extracts_human_message() -> None:
    """QA U-16 — raw `terraform validate -json` output must never reach the
    end user verbatim; extract summary/detail per diagnostic instead."""
    raw_json = (
        '{"format_version":"1.0","valid":false,"error_count":1,"warning_count":0,'
        '"diagnostics":[{"severity":"error",'
        '"summary":"Invalid Attribute Value Length",'
        '"detail":"expected length of name to be in the range (1 - 50), got panasa-x-guardrail",'
        '"range":{"filename":"guardrails.tf","start":{"line":5}}}]}'
    )
    message = _parse_terraform_validate_diagnostics(raw_json, "")
    assert "Invalid Attribute Value Length" in message
    assert "expected length of name to be in the range" in message
    assert "guardrails.tf" in message
    # The raw JSON structure itself must not leak through.
    assert '"severity"' not in message
    assert '"diagnostics"' not in message


def test_parse_terraform_validate_diagnostics_falls_back_on_unparseable_output() -> None:
    message = _parse_terraform_validate_diagnostics("not json at all", "some stderr")
    assert "not json at all" in message
    assert "some stderr" in message


async def test_dynamodb_pitr_passes_when_no_tables_present() -> None:
    """Wizard Redesign QA A-03 — agent-level generate-iac never renders its
    own aws_dynamodb_table resources (the real application tables are the
    factory's own bootstrap Terraform), so this trivially passes for every
    real agent bundle — matching the same sparse-check pattern already used
    by iam_least_privilege_lambda_roles when an agent has no Lambda roles."""
    report = await _render_and_validate(_config())
    check = _checks_by_name(report)["dynamodb_pitr"]
    assert check.passed
    assert "no aws_dynamodb_table" in check.detail.lower()


async def test_dynamodb_pitr_catches_table_without_pitr() -> None:
    report = await _validate_raw(
        """
        resource "aws_dynamodb_table" "bad" {
          name     = "panasa-agent-x-some-table"
          hash_key = "id"
        }
        """
    )
    check = _checks_by_name(report)["dynamodb_pitr"]
    assert not check.passed
    assert "aws_dynamodb_table.bad" in check.detail
    assert not report.passed


async def test_dynamodb_pitr_passes_when_enabled() -> None:
    report = await _validate_raw(
        """
        resource "aws_dynamodb_table" "good" {
          name     = "panasa-agent-x-some-table"
          hash_key = "id"
          point_in_time_recovery {
            enabled = true
          }
        }
        """
    )
    check = _checks_by_name(report)["dynamodb_pitr"]
    assert check.passed, check.detail


async def test_tagging_catches_missing_tags() -> None:
    report = await _validate_raw(
        """
        resource "aws_sns_topic" "untagged" {
          name = "panasa-agent-x-topic"
        }
        """
    )
    check = _checks_by_name(report)["tagging"]
    assert not check.passed
    assert "untagged" in check.detail


async def test_tagging_catches_wrong_tag_values() -> None:
    report = await _validate_raw(
        """
        resource "aws_sns_topic" "mistagged" {
          name = "panasa-agent-x-topic"
          tags = {
            agent_id   = "some-other-agent"
            tenant_id  = "tenant-a"
            version    = "1"
            managed_by = "panasa"
          }
        }
        """
    )
    check = _checks_by_name(report)["tagging"]
    assert not check.passed
    assert "mistagged" in check.detail


async def test_iam_wildcard_action_is_flagged() -> None:
    report = await _validate_raw(
        """
        resource "aws_iam_role_policy" "too_broad" {
          name = "panasa-agent-x-broad"
          role = aws_iam_role.some_role.id
          policy = jsonencode({
            Statement = [{
              Effect   = "Allow"
              Action   = "*"
              Resource = "some-arn"
            }]
          })
        }
        """
    )
    check = _checks_by_name(report)["iam_no_wildcard_actions_or_resources"]
    assert not check.passed
    assert "too_broad" in check.detail


async def test_iam_bare_wildcard_resource_is_flagged() -> None:
    report = await _validate_raw(
        """
        resource "aws_iam_role_policy" "too_broad" {
          name = "panasa-agent-x-broad"
          role = aws_iam_role.some_role.id
          policy = jsonencode({
            Statement = [{
              Effect   = "Allow"
              Action   = "s3:GetObject"
              Resource = "*"
            }]
          })
        }
        """
    )
    check = _checks_by_name(report)["iam_no_wildcard_actions_or_resources"]
    assert not check.passed


async def test_iam_scoped_arn_prefix_wildcard_is_not_flagged() -> None:
    """Resource = "${...}/*" (scoped under a specific bucket/prefix) is
    normal least-privilege shape, not a violation — only a bare "*" is."""
    report = await _validate_raw(
        """
        resource "aws_iam_role_policy" "scoped" {
          name = "panasa-agent-x-scoped"
          role = aws_iam_role.some_role.id
          policy = jsonencode({
            Statement = [{
              Effect   = "Allow"
              Action   = "s3:PutObject"
              Resource = "${aws_s3_bucket.x.arn}/*"
            }]
          })
        }
        """
    )
    check = _checks_by_name(report)["iam_no_wildcard_actions_or_resources"]
    assert check.passed, check.detail


async def test_iam_xray_resource_wildcard_is_exempt() -> None:
    """X-Ray's Put* actions have no resource-level IAM permissions at all —
    a documented AWS API constraint, not a least-privilege gap (matches
    observability.tf.j2's own agent_xray_access policy)."""
    report = await _validate_raw(
        """
        resource "aws_iam_role_policy" "xray" {
          name = "panasa-agent-x-xray"
          role = aws_iam_role.some_role.id
          policy = jsonencode({
            Statement = [{
              Effect   = "Allow"
              Action   = ["xray:PutTraceSegments", "xray:PutTelemetryRecords"]
              Resource = "*"
            }]
          })
        }
        """
    )
    check = _checks_by_name(report)["iam_no_wildcard_actions_or_resources"]
    assert check.passed, check.detail


async def test_lambda_execution_role_least_privilege_catches_wildcard() -> None:
    report = await _validate_raw(
        """
        resource "aws_iam_role" "tool_role" {
          name = "panasa-agent-x-tool-role"
          assume_role_policy = jsonencode({
            Statement = [{ Principal = { Service = "lambda.amazonaws.com" } }]
          })
        }

        resource "aws_iam_role_policy" "tool_policy" {
          name = "panasa-agent-x-tool-policy"
          role = aws_iam_role.tool_role.id
          policy = jsonencode({
            Statement = [{
              Effect   = "Allow"
              Action   = "*"
              Resource = "*"
            }]
          })
        }
        """
    )
    check = _checks_by_name(report)["iam_least_privilege_lambda_roles"]
    assert not check.passed
    assert "tool_role" in check.detail


async def test_security_group_flags_non_443_open_ingress() -> None:
    report = await _validate_raw(
        """
        resource "aws_security_group" "open" {
          name = "panasa-agent-x-sg"
          ingress {
            from_port   = 22
            to_port     = 22
            protocol    = "tcp"
            cidr_blocks = ["0.0.0.0/0"]
          }
        }
        """
    )
    check = _checks_by_name(report)["security_group_ingress"]
    assert not check.passed
    assert "22" in check.detail


async def test_security_group_allows_443_open_ingress() -> None:
    report = await _validate_raw(
        """
        resource "aws_security_group" "https_only" {
          name = "panasa-agent-x-sg"
          ingress {
            from_port   = 443
            to_port     = 443
            protocol    = "tcp"
            cidr_blocks = ["0.0.0.0/0"]
          }
        }
        """
    )
    check = _checks_by_name(report)["security_group_ingress"]
    assert check.passed, check.detail


async def test_s3_bucket_without_public_access_block_is_flagged() -> None:
    report = await _validate_raw(
        """
        resource "aws_s3_bucket" "naked" {
          bucket = "panasa-agent-x-naked"
        }
        """
    )
    check = _checks_by_name(report)["s3_block_public_access"]
    assert not check.passed
    assert "naked" in check.detail


async def test_s3_bucket_with_incomplete_public_access_block_is_flagged() -> None:
    report = await _validate_raw(
        """
        resource "aws_s3_bucket" "partial" {
          bucket = "panasa-agent-x-partial"
        }
        resource "aws_s3_bucket_public_access_block" "partial" {
          bucket                  = aws_s3_bucket.partial.id
          block_public_acls       = true
          block_public_policy     = false
          ignore_public_acls      = true
          restrict_public_buckets = true
        }
        """
    )
    check = _checks_by_name(report)["s3_block_public_access"]
    assert not check.passed


async def test_hardcoded_secret_literal_is_flagged() -> None:
    report = await _validate_raw(
        """
        resource "aws_lambda_function" "leaky" {
          function_name = "panasa-agent-x-leaky"
          environment {
            variables = {
              DB_PASSWORD = "hunter2"
            }
          }
        }
        """
    )
    check = _checks_by_name(report)["no_hardcoded_secrets"]
    assert not check.passed
    assert "DB_PASSWORD" in check.detail


async def test_secret_arn_reference_is_not_flagged() -> None:
    report = await _validate_raw(
        """
        resource "aws_lambda_function" "clean" {
          function_name = "panasa-agent-x-clean"
          environment {
            variables = {
              CREDENTIALS_SECRET_ARN = "arn:aws:secretsmanager:eu-west-2:123456789012:secret:x"
            }
          }
        }
        """
    )
    check = _checks_by_name(report)["no_hardcoded_secrets"]
    assert check.passed, check.detail


async def test_hcl_parse_error_is_reported_as_failed_check() -> None:
    report = await _validate_raw('resource "aws_s3_bucket" "broken" {\n  bucket = \n')
    assert not report.passed
    assert len(report.checks) == 1
    assert report.checks[0].name == "hcl_parse"
    assert not report.checks[0].passed


# ── CLAUDE.md Section 35.1's IaC Validation Test Matrix, row by row ──────
# R41 extended: a new AgentConfiguration feature flag must add a row here
# (and to the matrix in CLAUDE.md) before merging.

_ALL_RESOURCE_MARKERS = {
    "rag": "aws_opensearchserverless_collection",
    "tools": 'resource "aws_lambda_function"',
    "guardrails": "aws_bedrock_guardrail",
    "human_loop": "aws_sqs_queue",
    "audit": "aws_s3_bucket_object_lock_configuration",  # the WORM marker, unique to audit
}


# GuardrailConfig's own defaults (prompt_injection/pii_detection/
# toxicity_filter) are all True (a deliberate security default, unrelated
# to this matrix) — so the "guardrails" module is present even on an
# otherwise-bare config unless explicitly turned off here, for every row
# except "+Guardrails"/"All features" where it's the point.
_GUARDRAILS_OFF = {"prompt_injection": False, "pii_detection": False, "toxicity_filter": False}


def _rendered_text(config: AgentConfiguration) -> tuple[list[str], str]:
    modules = resolve_required_modules(config)
    files = _backend.render(_AGENT_ID, _TENANT_ID, _VERSION, config, modules)
    return modules, "\n".join(files.values())


def _assert_contains_only(modules: list[str], joined: str, *, present: set[str]) -> None:
    for feature, marker in _ALL_RESOURCE_MARKERS.items():
        if feature in present:
            assert marker in joined, f"expected {feature!r} marker {marker!r} to be present"
        else:
            assert marker not in joined, f"expected {feature!r} marker {marker!r} to be absent"


async def test_matrix_row_model_only() -> None:
    modules, joined = _rendered_text(_config(audit_enabled=False, guardrails=_GUARDRAILS_OFF))
    assert set(modules) == {
        "base",
        "api_gateway",
        "authentication",
        "compute",
        "observability",
    }
    _assert_contains_only(modules, joined, present=set())


async def test_matrix_row_plus_knowledge_base() -> None:
    modules, joined = _rendered_text(
        _config(
            audit_enabled=False,
            guardrails=_GUARDRAILS_OFF,
            knowledge_base={"enabled": True, "kb_name": "docs", "s3_bucket": "kb-bucket"},
        )
    )
    assert "rag" in modules
    _assert_contains_only(modules, joined, present={"rag"})


async def test_matrix_row_plus_tools_one_http() -> None:
    modules, joined = _rendered_text(
        _config(
            audit_enabled=False,
            guardrails=_GUARDRAILS_OFF,
            tools=[
                {
                    "tool_id": "jira",
                    "tool_name": "Jira",
                    "executor_type": "http",
                    "endpoint": "https://acme.atlassian.net",
                }
            ],
        )
    )
    assert "tools" in modules
    _assert_contains_only(modules, joined, present={"tools"})
    assert joined.count('resource "aws_lambda_function"') == 1


async def test_matrix_row_plus_guardrails() -> None:
    modules, joined = _rendered_text(
        _config(
            audit_enabled=False,
            guardrails={"prompt_injection": True, "pii_detection": True, "toxicity_filter": True},
        )
    )
    assert "guardrails" in modules
    _assert_contains_only(modules, joined, present={"guardrails"})


async def test_matrix_row_plus_human_review() -> None:
    modules, joined = _rendered_text(
        _config(
            audit_enabled=False,
            guardrails=_GUARDRAILS_OFF,
            human_review={"enabled": True, "trigger_conditions": ["high_risk_decision"]},
        )
    )
    assert "human_loop" in modules
    _assert_contains_only(modules, joined, present={"human_loop"})


async def test_matrix_row_plus_audit() -> None:
    modules, joined = _rendered_text(
        _config(guardrails=_GUARDRAILS_OFF)
    )  # audit_enabled defaults True
    assert "audit" in modules
    _assert_contains_only(modules, joined, present={"audit"})


async def test_matrix_row_all_features() -> None:
    config = _config(
        knowledge_base={"enabled": True, "kb_name": "docs", "s3_bucket": "kb-bucket"},
        tools=[
            {
                "tool_id": "jira",
                "tool_name": "Jira",
                "executor_type": "http",
                "endpoint": "https://acme.atlassian.net",
            }
        ],
        guardrails={"prompt_injection": True, "pii_detection": True, "toxicity_filter": True},
        human_review={"enabled": True, "trigger_conditions": ["high_risk_decision"]},
        # audit_enabled defaults True
    )
    modules, joined = _rendered_text(config)
    _assert_contains_only(
        modules, joined, present={"rag", "tools", "guardrails", "human_loop", "audit"}
    )

    report = await _render_and_validate(config)
    failed = [c for c in report.checks if not c.passed]
    assert not failed, failed
    assert report.passed
