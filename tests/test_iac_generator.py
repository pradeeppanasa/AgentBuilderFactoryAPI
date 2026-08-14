"""Tests for the pluggable IaC backends and the generator orchestrator.

Phase 6 deliverable per CLAUDE.md Section 14: "agent with no KB generates no
OpenSearch. Agent with no tools generates no Lambda." — checked here for
BOTH backends (Terraform + CDK), since both are real, selectable
implementations of the same IaCBackend interface (IAC_TOOL setting).
"""

from __future__ import annotations

import io
import zipfile
from typing import Any

import boto3
import pytest

from app.config import settings
from app.modules.iac_generator.backends.cdk import CDKBackend
from app.modules.iac_generator.backends.terraform import TerraformBackend
from app.modules.iac_generator.conditional import resolve_required_modules
from app.modules.iac_generator.generator import IaCGenerator
from app.modules.registry.models import AgentConfiguration
from tests.conftest import TEST_IAC_BUCKET

terraform_backend = TerraformBackend()
cdk_backend = CDKBackend()


def _config(**overrides: Any) -> AgentConfiguration:
    data: dict[str, Any] = {
        "model_id": "anthropic.claude-3-5-sonnet-20241022-v2:0",
        "model_provider": "bedrock",
        "system_prompt": "You are a helpful agent.",
    }
    data.update(overrides)
    return AgentConfiguration(**data)


def _one_tool() -> dict[str, Any]:
    return {
        "tool_id": "jira",
        "tool_name": "Jira",
        "executor_type": "http",
        "endpoint": "https://example.atlassian.net",
        "input_schema": {},
    }


# ── Terraform backend ────────────────────────────────────────────────────


def test_terraform_no_kb_generates_no_opensearch() -> None:
    config = _config()
    modules = resolve_required_modules(config)
    files = terraform_backend.render("agent-1", 1, config, modules)
    assert not any("opensearch" in content.lower() for content in files.values())


def test_terraform_kb_enabled_generates_opensearch() -> None:
    config = _config(knowledge_base={"enabled": True, "kb_name": "docs"})
    modules = resolve_required_modules(config)
    files = terraform_backend.render("agent-1", 1, config, modules)
    assert any("aws_opensearchserverless_collection" in content for content in files.values())


def test_terraform_no_tools_generates_no_lambda() -> None:
    config = _config(tools=[])
    modules = resolve_required_modules(config)
    files = terraform_backend.render("agent-1", 1, config, modules)
    assert not any("aws_lambda_function" in content for content in files.values())


def test_terraform_tools_configured_generates_lambda_per_tool() -> None:
    config = _config(tools=[_one_tool(), {**_one_tool(), "tool_id": "slack", "tool_name": "Slack"}])
    modules = resolve_required_modules(config)
    files = terraform_backend.render("agent-1", 1, config, modules)
    joined = "\n".join(files.values())
    assert joined.count('resource "aws_lambda_function"') == 2
    assert "tool_jira" in joined
    assert "tool_slack" in joined


def test_terraform_output_files_are_scoped_to_agent_and_module() -> None:
    config = _config(tools=[_one_tool()])
    modules = resolve_required_modules(config)
    files = terraform_backend.render("agent-42", 3, config, modules)
    assert all(path.startswith("terraform/agents/agent-42/") for path in files)
    assert any("/tools/" in path for path in files)


# ── CDK backend ──────────────────────────────────────────────────────────


def test_cdk_no_kb_generates_no_opensearch() -> None:
    config = _config()
    modules = resolve_required_modules(config)
    files = cdk_backend.render("agent-1", 1, config, modules)
    assert not any("opensearch" in content.lower() for content in files.values())


def test_cdk_kb_enabled_generates_opensearch() -> None:
    config = _config(knowledge_base={"enabled": True, "kb_name": "docs"})
    modules = resolve_required_modules(config)
    files = cdk_backend.render("agent-1", 1, config, modules)
    assert any("opensearchserverless.CfnCollection" in content for content in files.values())


def test_cdk_no_tools_generates_no_lambda_function() -> None:
    config = _config(tools=[])
    modules = resolve_required_modules(config)
    files = cdk_backend.render("agent-1", 1, config, modules)
    # "tools" module isn't resolved at all when there are no tools.
    assert not any(path.endswith("tools_stack.py") for path in files)
    assert not any("lambda_.Function" in content for content in files.values())


def test_cdk_tools_configured_generates_lambda_per_tool() -> None:
    config = _config(tools=[_one_tool(), {**_one_tool(), "tool_id": "slack", "tool_name": "Slack"}])
    modules = resolve_required_modules(config)
    files = cdk_backend.render("agent-1", 1, config, modules)
    joined = "\n".join(files.values())
    assert joined.count("lambda_.Function(") == 2


def test_cdk_generated_python_is_syntactically_valid() -> None:
    config = _config(
        knowledge_base={"enabled": True, "kb_name": "docs"},
        tools=[_one_tool()],
        human_review={"enabled": True, "trigger_conditions": ["high_risk_decision"]},
        guardrails={
            "pii_detection": True,
            "toxicity_filter": True,
            "topic_filter": True,
            "blocked_topics": ["politics"],
        },
        audit_enabled=True,
    )
    modules = resolve_required_modules(config)
    files = cdk_backend.render("agent-1", 1, config, modules)

    py_files = {path: content for path, content in files.items() if path.endswith(".py")}
    assert py_files, "expected at least one generated Python file"
    for path, content in py_files.items():
        compile(content, path, "exec")  # raises SyntaxError if the template produced bad code


def test_cdk_app_entrypoint_only_imports_resolved_modules() -> None:
    config = _config(tools=[])  # no KB, no tools, no human review, default audit_enabled=True
    modules = resolve_required_modules(config)
    files = cdk_backend.render("agent-7", 1, config, modules)
    app_py = files["cdk/agents/agent-7/app.py"]
    assert "add_audit_resources" in app_py
    assert "add_rag_resources" not in app_py
    assert "add_tools_resources" not in app_py
    assert "add_human_loop_resources" not in app_py


# ── Generator orchestrator (end-to-end with a mocked S3 bucket) ─────────


@pytest.fixture(autouse=True)
def _force_terraform_backend(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "iac_tool", "terraform")


async def test_generator_uploads_zip_and_returns_metadata() -> None:
    s3_client = boto3.client("s3", region_name="eu-west-2")
    generator = IaCGenerator(s3_client, settings)
    config = _config(tools=[_one_tool()])

    result = await generator.generate("agent-99", 2, config)

    assert result.tool == "terraform"
    assert result.iac_version == "1.0.2"
    assert "tools" in result.modules
    assert result.s3_key.startswith("iac/terraform/agent-99/v2/")

    obj = s3_client.get_object(Bucket=TEST_IAC_BUCKET, Key=result.s3_key)
    with zipfile.ZipFile(io.BytesIO(obj["Body"].read())) as archive:
        names = archive.namelist()
        assert any(name.startswith("terraform/agents/agent-99/tools/") for name in names)


async def test_generator_switches_backend_via_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "iac_tool", "cdk")
    s3_client = boto3.client("s3", region_name="eu-west-2")
    generator = IaCGenerator(s3_client, settings)

    result = await generator.generate("agent-100", 1, _config())

    assert result.tool == "cdk"
    assert result.s3_key.startswith("iac/cdk/agent-100/v1/")

    obj = s3_client.get_object(Bucket=TEST_IAC_BUCKET, Key=result.s3_key)
    with zipfile.ZipFile(io.BytesIO(obj["Body"].read())) as archive:
        assert "cdk/agents/agent-100/app.py" in archive.namelist()


async def test_generator_raises_when_bucket_not_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "iac_output_bucket", None)
    s3_client = boto3.client("s3", region_name="eu-west-2")
    generator = IaCGenerator(s3_client, settings)

    with pytest.raises(RuntimeError, match="IAC_OUTPUT_BUCKET"):
        await generator.generate("agent-1", 1, _config())
