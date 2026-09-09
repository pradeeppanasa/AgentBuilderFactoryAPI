"""Sprint 4 Phase 9 (S-10) — base/network.tf.j2 egress-allowlist rendering.

CLAUDE.md F6 documents 8 required egress categories; before this phase only
Bedrock (unconditional), OpenSearch/KB, web_search/url_reader skills, and
per-tool endpoints were actually generated (confirmed against the real
template — see the module docstring in
app/modules/knowledge_base/ingestion_scan.py-style commentary in
network.tf.j2 itself). This closes the DynamoDB/S3/CloudWatch gap.

Renders the real Jinja2 template via TerraformBackend.render() — not a
hand-written HCL fixture — so these tests fail if the template regresses.
"""

from __future__ import annotations

from app.modules.iac_generator.backends.terraform import TerraformBackend
from app.modules.iac_generator.conditional import resolve_required_modules
from app.modules.registry.models import AgentConfiguration, KBConfig

_NETWORK_FILE = "terraform/agents/agent-1/base__network.tf"


def _render(config: AgentConfiguration) -> str:
    backend = TerraformBackend()
    resolved_modules = resolve_required_modules(config)
    files = backend.render("agent-1", "tenant-a", 1, config, resolved_modules)
    return files[_NETWORK_FILE]


def _config(**overrides: object) -> AgentConfiguration:
    defaults: dict[str, object] = {
        "model_id": "anthropic.claude-3-5-sonnet-20241022-v2:0",
        "model_provider": "bedrock",
        "system_prompt": "You are a test agent.",
    }
    defaults.update(overrides)
    return AgentConfiguration(**defaults)


def test_dynamodb_egress_always_rendered() -> None:
    """Unconditional — every agent reads its own config + checks tenant_id
    on every request (R67), regardless of any other config flag."""
    network_tf = _render(_config(audit_enabled=False, observability_enabled=False))

    assert 'variable "dynamodb_endpoint_cidr"' in network_tf
    assert "cidr_blocks = [var.dynamodb_endpoint_cidr]" in network_tf


def test_s3_egress_rendered_when_audit_enabled() -> None:
    network_tf = _render(_config(audit_enabled=True))

    assert 'variable "s3_endpoint_cidr"' in network_tf
    assert "cidr_blocks = [var.s3_endpoint_cidr]" in network_tf


def test_s3_egress_absent_when_audit_disabled() -> None:
    network_tf = _render(_config(audit_enabled=False))

    assert 'variable "s3_endpoint_cidr"' not in network_tf
    assert "var.s3_endpoint_cidr" not in network_tf


def test_cloudwatch_egress_rendered_when_observability_enabled() -> None:
    network_tf = _render(_config(observability_enabled=True))

    assert 'variable "cloudwatch_endpoint_cidr"' in network_tf
    assert "cidr_blocks = [var.cloudwatch_endpoint_cidr]" in network_tf


def test_cloudwatch_egress_absent_when_observability_disabled() -> None:
    network_tf = _render(_config(observability_enabled=False))

    assert 'variable "cloudwatch_endpoint_cidr"' not in network_tf
    assert "var.cloudwatch_endpoint_cidr" not in network_tf


def test_all_egress_categories_present_for_a_fully_featured_agent() -> None:
    """Bedrock + DynamoDB always on; KB/audit/observability all enabled ->
    every F6 category this phase closes should appear together."""
    network_tf = _render(
        _config(
            audit_enabled=True,
            observability_enabled=True,
            knowledge_base=KBConfig(enabled=True, kb_id="kb-1", kb_name="Docs"),
        )
    )

    assert "var.bedrock_endpoint_cidr" in network_tf
    assert "var.dynamodb_endpoint_cidr" in network_tf
    assert "var.opensearch_endpoint_cidr" in network_tf
    assert "var.s3_endpoint_cidr" in network_tf
    assert "var.cloudwatch_endpoint_cidr" in network_tf
