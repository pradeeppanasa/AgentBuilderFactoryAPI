"""Generates scripts/panasa_policy_check.py — the real IaCValidator's static
HCL checks (naming, tagging, IAM least-privilege, security-group ingress,
S3 public access, hardcoded secrets, DynamoDB PITR) plus real `terraform
fmt`/`validate`, vendored into a single dependency-light, standalone script
committed alongside the generated Terraform (Generic Agent Runtime
instruction, 2026-09-03, item 5 — "replace 'panasa-policy-check .' with the
existing IaCValidator python script already in the codebase").

Why vendored rather than imported: the customer's own CI/CD runs this
(R57 — Panasa never touches customer infrastructure, and by the same
token never needs runtime access from it either), so it can't `pip
install` the Factory Runtime's own private package. The checks below are
copied from app/modules/iac_generator/validator.py, not reimplemented —
keep the two in sync by hand when either changes; there is deliberately no
import between them (that traffic would have to cross the R57 boundary
this script exists to respect).

Two adaptations from the original, both required to make it stand alone:
  - `_check_resource_presence` no longer takes an AgentConfiguration (the
    CI runner doesn't have one) — it infers which conditional modules
    (rag/tools/human_loop) were expected from the generated files' own
    {module}__{file}.tf naming convention (backends/terraform.py) instead,
    which carries exactly the same signal.
  - `terraform fmt`/`validate` run directly against the working directory
    on disk (already flat — backends/terraform.py's own layout) rather
    than validator.py's in-memory-dict-plus-tempdir dance, which exists
    there only because that caller has generated-but-not-yet-committed
    content with no files on disk yet.

Regenerated every deploy (unlike the CI/CD workflow file itself) — this is
pure, agent-independent code with nothing to preserve between deploys, and
should stay current with whatever this Runtime's own checks currently are.
"""

from __future__ import annotations

_POLICY_CHECK_SCRIPT = '''\
"""Panasa policy check — standalone port of this platform's real
IaCValidator (app/modules/iac_generator/validator.py). Regenerated on every
deploy; do not hand-edit — your changes will be silently overwritten on the
next deploy. Requires: pip install python-hcl2.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import hcl2

_LAMBDA_ASSUME_SERVICE = "lambda.amazonaws.com"

_RESOURCE_WILDCARD_EXEMPT_ACTIONS = frozenset(
    {
        "xray:PutTraceSegments",
        "xray:PutTelemetryRecords",
        "ecr:GetAuthorizationToken",
        "ecr:BatchCheckLayerAvailability",
        "ecr:GetDownloadUrlForLayer",
        "ecr:BatchGetImage",
        "bedrock:InvokeModel",
        "bedrock:InvokeModelWithResponseStream",
    }
)

_ID_HASH_LEN = 6


def _truncated_name(prefix: str, identifier: str, suffix: str, max_length: int) -> str:
    full = f"{prefix}{identifier}{suffix}"
    if len(full) <= max_length:
        return full
    import hashlib

    budget = max_length - len(prefix) - 1 - _ID_HASH_LEN - len(suffix)
    truncated = identifier[:budget]
    id_hash = hashlib.sha1(identifier.encode("utf-8")).hexdigest()[:_ID_HASH_LEN]
    return f"{prefix}{truncated}-{id_hash}{suffix}"


def _bedrock_guardrail_name(agent_id: str) -> str:
    return _truncated_name("panasa-", agent_id, "-guardrail", 50)


def _alb_name(agent_id: str) -> str:
    return _truncated_name("panasa-", agent_id, "-alb", 32)


def _target_group_name(agent_id: str) -> str:
    return _truncated_name("panasa-", agent_id, "-tg", 32)


def _opensearch_collection_name(agent_id: str) -> str:
    return _truncated_name("panasa-", agent_id, "-kb", 32)


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
    "aws_lb": "name",
    "aws_lb_target_group": "name",
}

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
        "aws_lb",
        "aws_lb_target_group",
    }
)
_REQUIRED_TAG_KEYS = frozenset({"agent_id", "tenant_id", "version", "managed_by"})

_KB_RESOURCE_TYPES = frozenset({"aws_opensearchserverless_collection", "aws_opensearch_domain"})

_SECRET_SHAPED_KEY_MARKERS = ("password", "secret", "api_key", "access_key", "private_key", "token")
_AWS_ACCESS_KEY_RE = re.compile(r"AKIA[0-9A-Z]{16}")
_PEM_HEADER_RE = re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")
_ACTION_WILDCARD_RE = re.compile(r'Action\\s*=\\s*(?:\\[\\s*)?"\\*"')
_RESOURCE_WILDCARD_RE = re.compile(r'Resource\\s*=\\s*(?:\\[\\s*)?"\\*"')
_ACTIONS_LIST_RE = re.compile(r"Action\\s*=\\s*\\[([^\\]]*)\\]")
_ACTION_SCALAR_RE = re.compile(r'Action\\s*=\\s*"([^"]*)"')


@dataclass
class CheckResult:
    name: str
    passed: bool
    detail: str


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


def _unquote(value: Any) -> Any:
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
        except Exception as exc:
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
    expected_truncated_names = {
        "aws_bedrock_guardrail": _bedrock_guardrail_name(agent_id),
        "aws_lb": _alb_name(agent_id),
        "aws_lb_target_group": _target_group_name(agent_id),
        "aws_opensearchserverless_collection": _opensearch_collection_name(agent_id),
    }
    violations = []
    for block in blocks:
        attr = _NAMEABLE_RESOURCE_TYPES.get(block.resource_type)
        if attr is None:
            continue
        value = block.attrs.get(attr)
        if not isinstance(value, str):
            continue
        expected = expected_truncated_names.get(block.resource_type)
        if expected is not None:
            if value != expected:
                violations.append(
                    f"{block.resource_type}.{block.resource_name}.{attr} = {value!r} "
                    f"(expected {expected!r})"
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


def _check_resource_presence(file_names: set[str], blocks: list[ResourceBlock]) -> CheckResult:
    # No AgentConfiguration available standalone here (R57) — the
    # {module}__{file}.tf naming convention this platform's Terraform
    # backend always generates (backends/terraform.py) carries the same
    # conditional-module signal a config object would.
    types_present = {b.resource_type for b in blocks}
    lambda_count = sum(1 for b in blocks if b.resource_type == "aws_lambda_function")

    kb_enabled = any(name.startswith("rag__") for name in file_names)
    has_tools = any(name.startswith("tools__") for name in file_names)
    human_review_enabled = any(name.startswith("human_loop__") for name in file_names)

    problems = []
    if kb_enabled and "aws_opensearchserverless_collection" not in types_present:
        problems.append("rag module present but no aws_opensearchserverless_collection")
    if not kb_enabled and (types_present & _KB_RESOURCE_TYPES):
        problems.append(f"no rag module but found {types_present & _KB_RESOURCE_TYPES}")

    if has_tools and lambda_count < 1:
        problems.append("tools module present but no aws_lambda_function resource(s) found")
    if not has_tools and lambda_count > 0:
        problems.append(f"no tools module but found {lambda_count} aws_lambda_function resource(s)")

    if human_review_enabled and "aws_sqs_queue" not in types_present:
        problems.append("human_loop module present but no aws_sqs_queue")
    if not human_review_enabled and "aws_sqs_queue" in types_present:
        problems.append("no human_loop module but found aws_sqs_queue")

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


def _run_terraform_fmt(tf_dir: Path) -> CheckResult:
    try:
        result = subprocess.run(
            ["terraform", "fmt", "-check", "-diff"],
            cwd=str(tf_dir),
            capture_output=True,
            text=True,
            timeout=30,
        )
    except FileNotFoundError:
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
        detail=f"terraform fmt found formatting issues:\\n{result.stdout}{result.stderr}"[:2000],
    )


def _parse_terraform_validate_diagnostics(stdout: str, stderr: str) -> str:
    fallback = f"terraform validate failed:\\n{stdout}{stderr}"[:2000]
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

    return "\\n".join(lines)[:2000] if lines else fallback


def _run_terraform_validate(tf_dir: Path) -> CheckResult:
    try:
        init_result = subprocess.run(
            ["terraform", "init", "-backend=false", "-input=false"],
            cwd=str(tf_dir),
            capture_output=True,
            text=True,
            timeout=120,
        )
    except FileNotFoundError:
        return CheckResult(
            name="terraform_validate", passed=True, detail="Skipped — terraform CLI not installed"
        )

    if init_result.returncode != 0:
        return CheckResult(
            name="terraform_validate",
            passed=False,
            detail=f"terraform init failed:\\n{(init_result.stderr or '')[:1500]}",
        )

    validate_result = subprocess.run(
        ["terraform", "validate", "-json"],
        cwd=str(tf_dir),
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


def main() -> int:
    tf_dir = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(".")
    metadata_path = tf_dir / "deployment-metadata.json"
    if not metadata_path.exists():
        print(f"FAIL: {metadata_path} not found — was this run from the right directory?")
        return 1
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    agent_id = metadata["agent_id"]
    tenant_id = metadata["tenant_id"]
    version = metadata["version"]

    tf_paths = sorted(tf_dir.glob("*.tf"))
    tf_files = {p.name: p.read_text(encoding="utf-8") for p in tf_paths}

    try:
        blocks = _parse_all_resources(tf_files)
    except HCLParseError as exc:
        print(f"FAIL hcl_parse: {exc}")
        return 1

    checks = [
        _check_resource_presence(set(tf_files), blocks),
        _check_naming_convention(blocks, agent_id),
        _check_tagging(blocks, agent_id, tenant_id, version),
        _check_iam_no_wildcards(blocks),
        _check_lambda_role_least_privilege(blocks),
        _check_security_group_ingress(blocks),
        _check_s3_public_access_block(blocks),
        _check_no_hardcoded_secrets(blocks),
        _check_dynamodb_pitr(blocks),
        _run_terraform_fmt(tf_dir),
        _run_terraform_validate(tf_dir),
    ]

    for check in checks:
        status = "PASS" if check.passed else "FAIL"
        print(f"{status} {check.name}: {check.detail}")

    passed = all(c.passed for c in checks)
    print()
    print("Policy check PASSED" if passed else "Policy check FAILED")
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
'''


def generate_policy_check_script() -> tuple[str, str]:
    """Returns (repo-relative file path, file content)."""
    return "scripts/panasa_policy_check.py", _POLICY_CHECK_SCRIPT
