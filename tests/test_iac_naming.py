"""Tests for app.modules.iac_generator.naming (QA I-02)."""

from __future__ import annotations

from app.modules.iac_generator.naming import bedrock_guardrail_name


def test_short_agent_id_is_not_truncated() -> None:
    name = bedrock_guardrail_name("kyc-agent-001")
    assert name == "panasa-kyc-agent-001-guardrail"


def test_long_agent_id_is_truncated_to_the_aws_limit() -> None:
    long_id = "kyc-document-verification-agent-3181e1"
    assert len(f"panasa-{long_id}-guardrail") == 55  # the real overflow reported in QA

    name = bedrock_guardrail_name(long_id)
    assert len(name) <= 50
    assert name.startswith("panasa-")
    assert name.endswith("-guardrail")


def test_truncation_is_deterministic() -> None:
    long_id = "kyc-document-verification-agent-3181e1"
    assert bedrock_guardrail_name(long_id) == bedrock_guardrail_name(long_id)


def test_two_long_ids_sharing_a_prefix_do_not_collide() -> None:
    id_a = "kyc-document-verification-agent-aaaaaa-tail-one"
    id_b = "kyc-document-verification-agent-aaaaaa-tail-two"
    assert bedrock_guardrail_name(id_a) != bedrock_guardrail_name(id_b)
