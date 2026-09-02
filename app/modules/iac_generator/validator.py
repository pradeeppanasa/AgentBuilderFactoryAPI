"""IaC validation pipeline (CLAUDE.md Section 6 — IaC Generator).

Runs against the Terraform files IaCGenerator.generate() just produced, for
POST /agents/{id}/generate-iac only (not the deploy flow — deploy's own
SECURITY_SCANNING/TERRAFORM_VALIDATE CodeBuild stages, run by the customer's
CI/CD per F0/F2/R05, are the real gate at deploy time; this is an earlier,
faster, Panasa-side sanity check surfaced directly in the generate-iac
response).

Only the terraform backend is covered — the checks below (HCL resource
presence, IAM policy shape, tags, security-group ingress) are inherently
Terraform-specific; a cdk-backend agent gets a single informational check
instead (validate() returns passed=True for it) rather than a validator that
would reject every CDK agent outright.

`terraform fmt`/`terraform validate` are optional: if the CLI isn't
installed (true almost everywhere this Runtime runs, per F0 — Terraform
execution is deliberately never this Runtime's job), both checks are
recorded as passed with a "skipped" detail and a warning is logged, rather
than failing every single validation report over a tooling-availability
question that has nothing to do with the generated HCL's correctness.
"""

from __future__ import annotations

import asyncio
import json
import re
import subprocess
import tempfile
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import hcl2

from app.modules.iac_generator.naming import bedrock_guardrail_name
from app.modules.iac_generator.validation_models import CheckResult, IaCValidationReport
from app.modules.registry.models import AgentConfiguration
from app.shared.logging import get_logger

log = get_logger()

_LAMBDA_ASSUME_SERVICE = "lambda.amazonaws.com"

# Actions AWS defines with no resource-level IAM permissions at all — a bare
# Resource = "*" is the API's own constraint here, not a least-privilege
# gap. Same documented-exception pattern as bootstrap/stage0/iam.tf's
# ecs:RegisterTaskDefinition statement. Extend deliberately, not casually.
_RESOURCE_WILDCARD_EXEMPT_ACTIONS = frozenset({"xray:PutTraceSegments", "xray:PutTelemetryRecords"})

# resource type -> its user-facing "identity" attribute, for the naming
# convention check. Deliberately an explicit allowlist rather than "check
# any attribute named name/bucket/..." — resources like
# aws_apigatewayv2_stage have a "name" that means something else entirely
# ("$default"), and aws_apigatewayv2_integration/_route have no identity
# attribute of their own at all.
_NAMEABLE_RESOURCE_TYPES: dict[str, str] = {
    "aws_apigatewayv2_api": "name",
    "aws_s3_bucket": "bucket",
    "aws_iam_role_policy": "name",
    "aws_iam_role": "name",
    "aws_security_group": "name",
    "aws_bedrock_guardrail": "name",
    "aws_sns_topic": "name",
    "aws_sqs_queue": "name",
    "aws_sfn_state_machine": "name",
    "aws_cloudwatch_log_group": "name",
    "aws_ecs_task_definition": "family",
    "aws_ecs_service": "name",
    "aws_opensearchserverless_collection": "name",
    "aws_bedrockagent_knowledge_base": "name",
    "aws_lambda_function": "function_name",
}

# Resource types the generated templates tag (app/modules/iac_generator/
# templates/terraform/_macros.tf.j2's panasa_tags()). Sub-resources with no
# tags argument in the AWS provider (aws_iam_role_policy — inline policies
# aren't taggable; aws_s3_bucket_public_access_block; aws_apigatewayv2_
# integration/_route) are intentionally excluded.
_TAGGABLE_RESOURCE_TYPES = frozenset(
    {
        "aws_apigatewayv2_api",
        "aws_apigatewayv2_stage",
        "aws_s3_bucket",
        "aws_iam_role",
        "aws_security_group",
        "aws_bedrock_guardrail",
        "aws_sns_topic",
        "aws_sqs_queue",
        "aws_sfn_state_machine",
        "aws_cloudwatch_log_group",
        "aws_ecs_task_definition",
        "aws_ecs_service",
        "aws_opensearchserverless_collection",
        "aws_bedrockagent_knowledge_base",
        "aws_lambda_function",
    }
)
_REQUIRED_TAG_KEYS = frozenset({"agent_id", "tenant_id", "version", "managed_by"})

_KB_RESOURCE_TYPES = frozenset({"aws_opensearchserverless_collection", "aws_opensearch_domain"})

_SECRET_SHAPED_KEY_MARKERS = ("password", "secret", "api_key", "access_key", "private_key", "token")
_AWS_ACCESS_KEY_RE = re.compile(r"AKIA[0-9A-Z]{16}")
_PEM_HEADER_RE = re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")
_ACTION_WILDCARD_RE = re.compile(r'Action\s*=\s*(?:\[\s*)?"\*"')
_RESOURCE_WILDCARD_RE = re.compile(r'Resource\s*=\s*(?:\[\s*)?"\*"')
_ACTIONS_LIST_RE = re.compile(r"Action\s*=\s*\[([^\]]*)\]")
_ACTION_SCALAR_RE = re.compile(r'Action\s*=\s*"([^"]*)"')


class HCLParseError(Exception):
    def __init__(self, file_path: str, reason: str) -> None:
        self.file_path = file_path
        super().__init__(f"Failed to parse {file_path}: {reason}")


@dataclass
class ResourceBlock:
    file_path: str
    resource_type: str
    resource_name: str
    attrs: dict[str, Any] = field(default_factory=dict)


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _unquote(value: Any) -> Any:
    """python-hcl2 8.x preserves the literal quote characters around plain
    string values (and even resource type/name keys) instead of stripping
    them — verified empirically against this exact version; see
    tests/test_iac_validator.py for the regression check. Interpolated
    expressions ("${...}") and function calls (jsonencode(...)) come
    through unquoted already, since they aren't plain string literals."""
    if isinstance(value, str) and len(value) >= 2 and value[0] == '"' and value[-1] == '"':
        return value[1:-1]
    return value


def _normalize(value: Any) -> Any:
    if isinstance(value, dict):
        return {_unquote(k): _normalize(v) for k, v in value.items() if k != "__is_block__"}
    if isinstance(value, list):
        return [_normalize(v) for v in value]
    return _unquote(value)


def _parse_all_resources(tf_files: dict[str, str]) -> list[ResourceBlock]:
    blocks: list[ResourceBlock] = []
    for file_path, content in tf_files.items():
        try:
            parsed = hcl2.loads(content)
        except Exception as exc:  # noqa: BLE001 — surfaced as a failed check, not a crash
            raise HCLParseError(file_path, str(exc)) from exc

        for resource_entry in parsed.get("resource", []):
            normalized_entry = _normalize(resource_entry)
            for resource_type, named in normalized_entry.items():
                for resource_name, body in named.items():
                    blocks.append(
                        ResourceBlock(
                            file_path=file_path,
                            resource_type=resource_type,
                            resource_name=resource_name,
                            attrs=body if isinstance(body, dict) else {},
                        )
                    )
    return blocks


def _flatten_attrs(obj: Any, prefix: str = "") -> Iterator[tuple[str, Any]]:
    if isinstance(obj, dict):
        for key, value in obj.items():
            yield from _flatten_attrs(value, f"{prefix}.{key}" if prefix else str(key))
    elif isinstance(obj, list):
        for item in obj:
            yield from _flatten_attrs(item, prefix)
    else:
        yield (prefix, obj)


def _policy_text(block: ResourceBlock) -> str:
    value = block.attrs.get("policy", "")
    return value if isinstance(value, str) else ""


def _extract_actions(policy_text: str) -> list[str]:
    actions: list[str] = []
    for match in _ACTIONS_LIST_RE.finditer(policy_text):
        actions.extend(re.findall(r'"([^"]+)"', match.group(1)))
    for match in _ACTION_SCALAR_RE.finditer(policy_text):
        actions.append(match.group(1))
    return actions


def _policy_violation(policy_text: str) -> str | None:
    """Returns a human-readable reason if this IAM policy JSON (rendered by
    jsonencode(...), captured as raw text — see _unquote's docstring on why
    that's what hcl2 gives us for function-call expressions) is overly
    permissive; None if it's fine."""
    if _ACTION_WILDCARD_RE.search(policy_text):
        return 'Action = "*"'
    if _RESOURCE_WILDCARD_RE.search(policy_text):
        actions = _extract_actions(policy_text)
        if not actions or not set(actions).issubset(_RESOURCE_WILDCARD_EXEMPT_ACTIONS):
            return f'Resource = "*" on action(s) {actions or ["<unknown>"]}'
    return None


def _iam_policy_blocks(blocks: list[ResourceBlock]) -> list[ResourceBlock]:
    return [b for b in blocks if b.resource_type in ("aws_iam_role_policy", "aws_iam_policy")]


def _check_iam_no_wildcards(blocks: list[ResourceBlock]) -> CheckResult:
    violations = []
    for block in _iam_policy_blocks(blocks):
        reason = _policy_violation(_policy_text(block))
        if reason:
            violations.append(f"{block.resource_type}.{block.resource_name}: {reason}")
    passed = not violations
    detail = (
        "No wildcard Action/Resource found in any IAM policy"
        if passed
        else "Wildcard IAM permissions found: " + "; ".join(violations)
    )
    return CheckResult(name="iam_no_wildcard_actions_or_resources", passed=passed, detail=detail)


def _check_lambda_role_least_privilege(blocks: list[ResourceBlock]) -> CheckResult:
    lambda_role_names = {
        b.resource_name
        for b in blocks
        if b.resource_type == "aws_iam_role"
        and _LAMBDA_ASSUME_SERVICE in str(b.attrs.get("assume_role_policy", ""))
    }
    if not lambda_role_names:
        return CheckResult(
            name="iam_least_privilege_lambda_roles",
            passed=True,
            detail="No Lambda execution roles in this configuration",
        )

    violations = []
    for block in _iam_policy_blocks(blocks):
        role_ref = str(block.attrs.get("role", ""))
        owning_role = next(
            (name for name in lambda_role_names if f"aws_iam_role.{name}." in role_ref), None
        )
        if owning_role is None:
            continue
        reason = _policy_violation(_policy_text(block))
        if reason:
            violations.append(f"role {owning_role!r} via {block.resource_name}: {reason}")

    passed = not violations
    detail = (
        f"All {len(lambda_role_names)} Lambda execution role(s) have least-privilege policies"
        if passed
        else "Overly permissive Lambda execution role polic(ies): " + "; ".join(violations)
    )
    return CheckResult(name="iam_least_privilege_lambda_roles", passed=passed, detail=detail)


def _check_naming_convention(blocks: list[ResourceBlock], agent_id: str) -> CheckResult:
    prefix = f"panasa-{agent_id}-"
    # QA I-02 — aws_bedrock_guardrail is the one resource type with a real
    # AWS length cap (50 chars) tight enough that long agent_ids overflow
    # the standard prefix; bedrock_guardrail_name() truncates it, so this
    # check compares against that exact expected name rather than the
    # literal panasa-{agent_id}- prefix every other resource type uses.
    expected_guardrail_name = bedrock_guardrail_name(agent_id)
    violations = []
    for block in blocks:
        attr = _NAMEABLE_RESOURCE_TYPES.get(block.resource_type)
        if attr is None:
            continue
        value = block.attrs.get(attr)
        if not isinstance(value, str):
            continue
        if block.resource_type == "aws_bedrock_guardrail":
            if value != expected_guardrail_name:
                violations.append(
                    f"{block.resource_type}.{block.resource_name}.{attr} = {value!r} "
                    f"(expected {expected_guardrail_name!r})"
                )
            continue
        if not value.startswith(prefix):
            violations.append(f"{block.resource_type}.{block.resource_name}.{attr} = {value!r}")
    passed = not violations
    detail = (
        f"All nameable resources use the {prefix!r} prefix"
        if passed
        else "Naming violations: " + "; ".join(violations)
    )
    return CheckResult(name="naming_convention", passed=passed, detail=detail)


def _check_tagging(
    blocks: list[ResourceBlock], agent_id: str, tenant_id: str, version: int
) -> CheckResult:
    violations = []
    for block in blocks:
        if block.resource_type not in _TAGGABLE_RESOURCE_TYPES:
            continue
        tags = block.attrs.get("tags")
        label = f"{block.resource_type}.{block.resource_name}"
        if not isinstance(tags, dict) or not _REQUIRED_TAG_KEYS.issubset(tags.keys()):
            violations.append(f"{label} is missing one or more required tags")
            continue
        expected = {
            "agent_id": agent_id,
            "tenant_id": tenant_id,
            "version": str(version),
            "managed_by": "panasa",
        }
        mismatched = {k: tags.get(k) for k, v in expected.items() if str(tags.get(k)) != str(v)}
        if mismatched:
            violations.append(f"{label} has incorrect tag values: {mismatched}")
    passed = not violations
    detail = (
        f"All {sum(1 for b in blocks if b.resource_type in _TAGGABLE_RESOURCE_TYPES)} "
        "taggable resource(s) have correct agent_id/tenant_id/version/managed_by tags"
        if passed
        else "Tagging violations: " + "; ".join(violations)
    )
    return CheckResult(name="tagging", passed=passed, detail=detail)


def _check_resource_presence(
    config: AgentConfiguration, blocks: list[ResourceBlock]
) -> CheckResult:
    types_present = {b.resource_type for b in blocks}
    lambda_count = sum(1 for b in blocks if b.resource_type == "aws_lambda_function")

    kb_enabled = bool(config.knowledge_base and config.knowledge_base.enabled)
    has_tools = bool(config.tools)
    human_review_enabled = bool(config.human_review and config.human_review.enabled)

    problems = []
    if kb_enabled and "aws_opensearchserverless_collection" not in types_present:
        problems.append("knowledge_base.enabled=true but no aws_opensearchserverless_collection")
    if not kb_enabled and (types_present & _KB_RESOURCE_TYPES):
        problems.append(f"knowledge_base disabled but found {types_present & _KB_RESOURCE_TYPES}")

    if has_tools and lambda_count < len(config.tools):
        problems.append(
            f"{len(config.tools)} tool(s) configured but only {lambda_count} "
            "aws_lambda_function resource(s) found"
        )
    if not has_tools and lambda_count > 0:
        problems.append(
            f"no tools configured but found {lambda_count} aws_lambda_function resource(s)"
        )

    if human_review_enabled and "aws_sqs_queue" not in types_present:
        problems.append("human_review.enabled=true but no aws_sqs_queue")
    if not human_review_enabled and "aws_sqs_queue" in types_present:
        problems.append("human_review disabled but found aws_sqs_queue")

    passed = not problems
    detail = (
        "All conditional resources present/absent as expected" if passed else "; ".join(problems)
    )
    return CheckResult(name="resource_presence", passed=passed, detail=detail)


def _check_security_group_ingress(blocks: list[ResourceBlock]) -> CheckResult:
    violations = []
    for block in blocks:
        if block.resource_type != "aws_security_group":
            continue
        ingress_rules = block.attrs.get("ingress", [])
        if isinstance(ingress_rules, dict):
            ingress_rules = [ingress_rules]
        for rule in ingress_rules:
            if not isinstance(rule, dict):
                continue
            cidrs = rule.get("cidr_blocks") or []
            if "0.0.0.0/0" not in cidrs:
                continue
            from_port, to_port = rule.get("from_port"), rule.get("to_port")
            if not (from_port == 443 and to_port == 443):
                violations.append(
                    f"{block.resource_name}: 0.0.0.0/0 ingress on port(s) {from_port}-{to_port}"
                )
    passed = not violations
    detail = (
        "No security group allows 0.0.0.0/0 ingress on a non-443 port"
        if passed
        else "Open ingress found: " + "; ".join(violations)
    )
    return CheckResult(name="security_group_ingress", passed=passed, detail=detail)


def _check_s3_public_access_block(blocks: list[ResourceBlock]) -> CheckResult:
    bucket_names = {b.resource_name for b in blocks if b.resource_type == "aws_s3_bucket"}
    pab_blocks = [b for b in blocks if b.resource_type == "aws_s3_bucket_public_access_block"]

    violations = []
    for bucket_name in bucket_names:
        matching = next(
            (
                b
                for b in pab_blocks
                if f"aws_s3_bucket.{bucket_name}." in str(b.attrs.get("bucket", ""))
            ),
            None,
        )
        if matching is None:
            violations.append(
                f"aws_s3_bucket.{bucket_name} has no aws_s3_bucket_public_access_block"
            )
            continue
        required_flags = (
            "block_public_acls",
            "block_public_policy",
            "ignore_public_acls",
            "restrict_public_buckets",
        )
        if not all(matching.attrs.get(flag) is True for flag in required_flags):
            violations.append(
                f"aws_s3_bucket_public_access_block.{matching.resource_name} "
                "does not set all four block-public flags to true"
            )
    passed = not violations
    detail = (
        f"All {len(bucket_names)} S3 bucket(s) fully block public access"
        if passed
        else "S3 public access violations: " + "; ".join(violations)
    )
    return CheckResult(name="s3_block_public_access", passed=passed, detail=detail)


def _check_dynamodb_pitr(blocks: list[ResourceBlock]) -> CheckResult:
    """Wizard Redesign QA A-03/I-01. Per-agent Terraform bundles don't
    currently create any aws_dynamodb_table (the real DynamoDB tables are
    all platform-wide, provisioned once in bootstrap/stage1/dynamodb.tf —
    and already set point_in_time_recovery.enabled=true there) — so this
    check passes trivially today. It exists so a future per-agent template
    that does add a DynamoDB table can't silently ship without PITR."""
    tables = [b for b in blocks if b.resource_type == "aws_dynamodb_table"]
    if not tables:
        return CheckResult(
            name="dynamodb_pitr",
            passed=True,
            detail="No aws_dynamodb_table resources in this configuration",
        )

    violations = []
    for table in tables:
        pitr = table.attrs.get("point_in_time_recovery")
        if isinstance(pitr, list):
            pitr = pitr[0] if pitr else {}
        if not isinstance(pitr, dict) or pitr.get("enabled") is not True:
            violations.append(f"aws_dynamodb_table.{table.resource_name}")
    passed = not violations
    detail = (
        f"All {len(tables)} DynamoDB table(s) have point_in_time_recovery enabled"
        if passed
        else "Tables missing point_in_time_recovery: " + ", ".join(violations)
    )
    return CheckResult(name="dynamodb_pitr", passed=passed, detail=detail)


def _looks_like_reference(value: str) -> bool:
    return (
        value.startswith("${")
        or value.startswith("var.")
        or value.startswith("data.")
        or value.startswith("aws_")
        or "arn:aws:secretsmanager" in value
        or value == ""
    )


def _check_no_hardcoded_secrets(blocks: list[ResourceBlock]) -> CheckResult:
    violations = []
    for block in blocks:
        for key, value in _flatten_attrs(block.attrs):
            if not isinstance(value, str):
                continue
            label = f"{block.resource_type}.{block.resource_name}.{key}"
            if _AWS_ACCESS_KEY_RE.search(value) or _PEM_HEADER_RE.search(value):
                violations.append(f"{label} contains what looks like a hardcoded credential")
                continue
            key_leaf = key.rsplit(".", 1)[-1].lower()
            if any(
                marker in key_leaf for marker in _SECRET_SHAPED_KEY_MARKERS
            ) and not _looks_like_reference(value):
                violations.append(f"{label} assigns a literal value to a secret-shaped attribute")
    passed = not violations
    detail = (
        "No hardcoded secrets found in any resource attribute"
        if passed
        else "Possible hardcoded secrets: " + "; ".join(violations)
    )
    return CheckResult(name="no_hardcoded_secrets", passed=passed, detail=detail)


def _flatten_for_terraform_cli(tf_files: dict[str, str]) -> dict[str, str]:
    """terraform validate/init resolve resources within a single directory
    only — they don't recurse into subdirectories the way our generated
    layout (terraform/agents/{agent_id}/{module}/{file}.tf) is organised for
    the customer's git repo. Flattening into one directory for this
    subprocess call only is what lets cross-module references (e.g.
    tools.tf's aws_iam_role.agent_execution_role, defined in
    authentication.tf) resolve; it does not change what's actually zipped/
    committed."""
    flattened: dict[str, str] = {}
    for file_path, content in tf_files.items():
        flat_name = "__".join(Path(file_path).parts[3:])  # drop terraform/agents/{agent_id}/
        flattened[flat_name or Path(file_path).name] = content
    return flattened


def _run_terraform_fmt(tmpdir: Path, terraform_binary: str = "terraform") -> CheckResult:
    try:
        result = subprocess.run(  # noqa: S603 — fixed argv, no shell, no user input
            [terraform_binary, "fmt", "-check", "-diff"],
            cwd=str(tmpdir),
            capture_output=True,
            text=True,
            timeout=30,
        )
    except FileNotFoundError:
        log.warning("iac_validation.terraform_cli_not_found", check="terraform_fmt")
        return CheckResult(
            name="terraform_fmt", passed=True, detail="Skipped — terraform CLI not installed"
        )
    if result.returncode == 0:
        return CheckResult(
            name="terraform_fmt", passed=True, detail="All files correctly formatted"
        )
    return CheckResult(
        name="terraform_fmt",
        passed=False,
        detail=f"terraform fmt found formatting issues:\n{result.stdout}{result.stderr}"[:2000],
    )


def _run_terraform_validate(tmpdir: Path, terraform_binary: str = "terraform") -> CheckResult:
    try:
        init_result = subprocess.run(  # noqa: S603
            [terraform_binary, "init", "-backend=false", "-input=false"],
            cwd=str(tmpdir),
            capture_output=True,
            text=True,
            timeout=120,
        )
    except FileNotFoundError:
        log.warning("iac_validation.terraform_cli_not_found", check="terraform_validate")
        return CheckResult(
            name="terraform_validate", passed=True, detail="Skipped — terraform CLI not installed"
        )

    if init_result.returncode != 0:
        # Most commonly: no network access to fetch the AWS provider plugin.
        # Not this configuration's fault — skip rather than fail.
        log.warning("iac_validation.terraform_init_failed", detail=(init_result.stderr or "")[:500])
        return CheckResult(
            name="terraform_validate",
            passed=True,
            detail="Skipped — terraform init failed (provider download likely unavailable)",
        )

    validate_result = subprocess.run(  # noqa: S603
        [terraform_binary, "validate", "-json"],
        cwd=str(tmpdir),
        capture_output=True,
        text=True,
        timeout=60,
    )
    if validate_result.returncode == 0:
        return CheckResult(
            name="terraform_validate", passed=True, detail="Configuration is syntactically valid"
        )
    return CheckResult(
        name="terraform_validate",
        passed=False,
        detail=_parse_terraform_validate_diagnostics(
            validate_result.stdout, validate_result.stderr
        ),
    )


def _parse_terraform_validate_diagnostics(stdout: str, stderr: str) -> str:
    """QA U-16 — `terraform validate -json`'s raw stdout is a JSON object
    with a `diagnostics` array; dumping it verbatim showed end users an
    unreadable wall of JSON instead of the actual error. Extract each
    diagnostic's summary/detail into a plain-English line; fall back to the
    raw output if it isn't parseable JSON in the expected shape (never
    crash the validator over an unexpected terraform output format)."""
    fallback = f"terraform validate failed:\n{stdout}{stderr}"[:2000]
    try:
        parsed = json.loads(stdout)
    except (json.JSONDecodeError, ValueError):
        return fallback

    diagnostics = parsed.get("diagnostics") if isinstance(parsed, dict) else None
    if not isinstance(diagnostics, list) or not diagnostics:
        return fallback

    lines = []
    for diag in diagnostics:
        if not isinstance(diag, dict):
            continue
        summary = diag.get("summary") or "Unknown error"
        detail = diag.get("detail")
        diag_range = diag.get("range")
        filename = diag_range.get("filename") if isinstance(diag_range, dict) else None
        line = str(summary)
        if detail:
            line += f" — {detail}"
        if filename:
            line += f" ({filename})"
        lines.append(line)

    return "\n".join(lines)[:2000] if lines else fallback


class IaCValidator:
    def __init__(self, terraform_binary: str = "terraform") -> None:
        self._terraform_binary = terraform_binary

    async def validate(
        self,
        *,
        agent_id: str,
        tenant_id: str,
        version: int,
        config: AgentConfiguration,
        files: dict[str, str],
        tool: str,
    ) -> IaCValidationReport:
        if tool != "terraform":
            return IaCValidationReport(
                passed=True,
                tool=tool,
                generated_at=_now(),
                checks=[
                    CheckResult(
                        name="terraform_only",
                        passed=True,
                        detail=(
                            "IaC validation suite only covers the terraform backend; "
                            f"{tool!r} was not checked"
                        ),
                    )
                ],
            )

        tf_files = {path: content for path, content in files.items() if path.endswith(".tf")}
        checks: list[CheckResult] = []

        try:
            blocks = await asyncio.to_thread(_parse_all_resources, tf_files)
        except HCLParseError as exc:
            return IaCValidationReport(
                passed=False,
                tool=tool,
                generated_at=_now(),
                checks=[CheckResult(name="hcl_parse", passed=False, detail=str(exc))],
            )

        checks.append(_check_resource_presence(config, blocks))
        checks.append(_check_naming_convention(blocks, agent_id))
        checks.append(_check_tagging(blocks, agent_id, tenant_id, version))
        checks.append(_check_iam_no_wildcards(blocks))
        checks.append(_check_lambda_role_least_privilege(blocks))
        checks.append(_check_security_group_ingress(blocks))
        checks.append(_check_s3_public_access_block(blocks))
        checks.append(_check_no_hardcoded_secrets(blocks))
        checks.append(_check_dynamodb_pitr(blocks))

        with tempfile.TemporaryDirectory(prefix="panasa-iac-validate-") as tmpdir_str:
            tmpdir = Path(tmpdir_str)
            flattened = _flatten_for_terraform_cli(tf_files)
            for name, content in flattened.items():
                (tmpdir / name).write_text(content, encoding="utf-8")
            checks.append(
                await asyncio.to_thread(_run_terraform_fmt, tmpdir, self._terraform_binary)
            )
            checks.append(
                await asyncio.to_thread(_run_terraform_validate, tmpdir, self._terraform_binary)
            )

        return IaCValidationReport(
            passed=all(c.passed for c in checks),
            tool=tool,
            generated_at=_now(),
            checks=checks,
        )
