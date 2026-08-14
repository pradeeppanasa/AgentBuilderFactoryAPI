"""Unit tests for the Change Impact Analyzer (CLAUDE.md Section 7 / Phase 5).

These are pure unit tests against AgentConfiguration objects — no DB, no
HTTP, no auth — matching the "compares two AgentConfiguration objects"
scope of Phase 5's analyzer.
"""

from typing import Any

from app.modules.change_impact.analyzer import ChangeImpactAnalyzer
from app.modules.change_impact.rules import CRITICAL_BLOCK_CONDITIONS, IMPACT_RULES
from app.modules.registry.models import AgentConfiguration

analyzer = ChangeImpactAnalyzer()


def _config(**overrides: Any) -> AgentConfiguration:
    data: dict[str, Any] = {
        "model_id": "anthropic.claude-3-5-sonnet-20241022-v2:0",
        "model_provider": "bedrock",
        "system_prompt": "You are a helpful agent.",
    }
    data.update(overrides)
    return AgentConfiguration(**data)


# ── Rules table completeness (Section 7 verbatim) ───────────────────────────


def test_rules_table_matches_spec_exactly() -> None:
    assert IMPACT_RULES == {
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


def test_critical_block_conditions_match_spec_exactly() -> None:
    assert CRITICAL_BLOCK_CONDITIONS == [
        "hardcoded_secret_found",
        "critical_cve_found",
        "iam_privilege_escalation",
        "prompt_injection_vulnerability",
        "data_exfiltration_risk",
    ]


# ── One test per config-diff-derivable rule ─────────────────────────────────


def test_system_prompt_rule() -> None:
    result = analyzer.analyze(_config(), _config(system_prompt="You are an updated agent."))
    assert result.matched_rules == ["system_prompt"]
    assert result.impact_level == "MEDIUM"
    assert set(result.required_validations) == {"PROMPT_EVALUATION", "GUARDRAIL_TESTS"}


def test_model_id_rule() -> None:
    result = analyzer.analyze(_config(), _config(model_id="anthropic.claude-3-opus-20240229-v1:0"))
    assert result.matched_rules == ["model_id"]
    assert result.impact_level == "HIGH"
    assert set(result.required_validations) == {"MODEL_EVALUATION", "SAFETY_TESTS"}


def test_model_provider_rule() -> None:
    result = analyzer.analyze(_config(), _config(model_provider="azure_openai"))
    assert result.matched_rules == ["model_provider"]
    assert result.impact_level == "HIGH"
    assert set(result.required_validations) == {"MODEL_EVALUATION", "SAFETY_TESTS", "FULL_SECURITY"}


def test_temperature_rule() -> None:
    result = analyzer.analyze(_config(temperature=0.3), _config(temperature=0.9))
    assert result.matched_rules == ["temperature"]
    assert result.impact_level == "LOW"
    assert result.required_validations == ["PROMPT_EVALUATION"]


def test_max_tokens_rule() -> None:
    result = analyzer.analyze(_config(max_tokens=2048), _config(max_tokens=4096))
    assert result.matched_rules == ["max_tokens"]
    assert result.impact_level == "LOW"
    assert result.required_validations == ["PROMPT_EVALUATION"]


def test_knowledge_base_wildcard_rule_on_enable() -> None:
    to_cfg = _config(
        knowledge_base={"enabled": True, "kb_name": "docs", "s3_bucket": "customer-docs"}
    )
    result = analyzer.analyze(_config(), to_cfg)
    assert result.matched_rules == ["knowledge_base.*"]
    assert result.impact_level == "MEDIUM"
    assert result.required_validations == ["RAG_EVALUATION"]


def test_knowledge_base_wildcard_rule_on_sub_field_change() -> None:
    from_cfg = _config(knowledge_base={"enabled": True, "kb_name": "docs", "top_k": 5})
    to_cfg = _config(knowledge_base={"enabled": True, "kb_name": "docs", "top_k": 10})
    result = analyzer.analyze(from_cfg, to_cfg)
    assert result.matched_rules == ["knowledge_base.*"]
    assert result.impact_level == "MEDIUM"


def test_guardrails_wildcard_rule() -> None:
    from_cfg = _config(guardrails={"pii_detection": True})
    to_cfg = _config(guardrails={"pii_detection": False})
    result = analyzer.analyze(from_cfg, to_cfg)
    assert result.matched_rules == ["guardrails.*"]
    assert result.impact_level == "HIGH"
    assert result.required_validations == ["FULL_GUARDRAIL_REGRESSION"]


def test_tools_add_rule() -> None:
    to_cfg = _config(
        tools=[
            {
                "tool_id": "jira",
                "tool_name": "Jira",
                "executor_type": "http",
                "input_schema": {},
            }
        ]
    )
    result = analyzer.analyze(_config(tools=[]), to_cfg)
    assert result.matched_rules == ["tools[*].add"]
    assert result.impact_level == "HIGH"
    assert set(result.required_validations) == {"TOOL_SECURITY", "INTEGRATION_TESTS", "IAC_SCAN"}


def test_tools_remove_rule() -> None:
    from_cfg = _config(
        tools=[
            {
                "tool_id": "jira",
                "tool_name": "Jira",
                "executor_type": "http",
                "input_schema": {},
            }
        ]
    )
    result = analyzer.analyze(from_cfg, _config(tools=[]))
    assert result.matched_rules == ["tools[*].remove"]
    assert result.impact_level == "MEDIUM"
    assert result.required_validations == ["INTEGRATION_TESTS"]


def test_tools_endpoint_rule() -> None:
    def _tool(endpoint: str) -> dict[str, Any]:
        return {
            "tool_id": "jira",
            "tool_name": "Jira",
            "executor_type": "http",
            "endpoint": endpoint,
            "input_schema": {},
        }

    from_cfg = _config(tools=[_tool("https://old.example.com")])
    to_cfg = _config(tools=[_tool("https://new.example.com")])
    result = analyzer.analyze(from_cfg, to_cfg)
    assert result.matched_rules == ["tools[*].endpoint"]
    assert result.impact_level == "HIGH"
    assert set(result.required_validations) == {"TOOL_SECURITY", "INTEGRATION_TESTS"}


def test_tools_non_endpoint_field_change_does_not_match_any_rule() -> None:
    def _tool(name: str) -> dict[str, Any]:
        return {
            "tool_id": "jira",
            "tool_name": name,
            "executor_type": "http",
            "input_schema": {},
        }

    from_cfg = _config(tools=[_tool("Jira")])
    to_cfg = _config(tools=[_tool("Jira Cloud")])
    result = analyzer.analyze(from_cfg, to_cfg)
    assert result.matched_rules == []
    assert result.impact_level == "LOW"
    assert result.required_validations == []


def test_human_review_wildcard_rule_on_enable() -> None:
    to_cfg = _config(human_review={"enabled": True, "trigger_conditions": ["high_risk_decision"]})
    result = analyzer.analyze(_config(), to_cfg)
    assert result.matched_rules == ["human_review.*"]
    assert result.impact_level == "MEDIUM"
    assert result.required_validations == ["INTEGRATION_TESTS"]


def test_token_budget_daily_rule() -> None:
    result = analyzer.analyze(
        _config(token_budget_daily=1_000_000), _config(token_budget_daily=2_000_000)
    )
    assert result.matched_rules == ["token_budget_daily"]
    assert result.impact_level == "LOW"
    assert result.required_validations == []


def test_rate_limit_rpm_rule() -> None:
    result = analyzer.analyze(_config(rate_limit_rpm=60), _config(rate_limit_rpm=120))
    assert result.matched_rules == ["rate_limit_rpm"]
    assert result.impact_level == "LOW"
    assert result.required_validations == []


# ── Combinations and edge cases ─────────────────────────────────────────────


def test_no_changes_yields_low_and_empty() -> None:
    result = analyzer.analyze(_config(), _config())
    assert result == analyzer.analyze(_config(), _config())
    assert result.impact_level == "LOW"
    assert result.matched_rules == []
    assert result.required_validations == []


def test_unmatched_field_change_yields_low_and_empty() -> None:
    # top_p has no rule in the table at all.
    result = analyzer.analyze(_config(top_p=0.9), _config(top_p=0.5))
    assert result.matched_rules == []
    assert result.impact_level == "LOW"
    assert result.required_validations == []


def test_impact_level_is_the_max_of_all_matched_rules() -> None:
    # temperature (LOW) + model_id (HIGH) changed together -> overall HIGH.
    from_cfg = _config(temperature=0.3)
    to_cfg = _config(temperature=0.9, model_id="anthropic.claude-3-opus-20240229-v1:0")
    result = analyzer.analyze(from_cfg, to_cfg)
    assert set(result.matched_rules) == {"temperature", "model_id"}
    assert result.impact_level == "HIGH"
    assert set(result.required_validations) == {
        "PROMPT_EVALUATION",
        "MODEL_EVALUATION",
        "SAFETY_TESTS",
    }


def test_initial_version_creation_does_not_falsely_match_unset_optional_blocks() -> None:
    # from_config=None (v1 creation): knowledge_base/human_review default to
    # None and must not be treated as a "change" to those subsystems just
    # because the key is newly present in the diff.
    result = analyzer.analyze(None, _config())
    assert "knowledge_base.*" not in result.matched_rules
    assert "human_review.*" not in result.matched_rules
    # But the always-present required fields are legitimately "new".
    assert set(result.matched_rules) >= {"system_prompt", "model_id", "model_provider"}
    assert result.impact_level == "HIGH"


def test_initial_version_creation_with_knowledge_base_enabled_does_match() -> None:
    to_cfg = _config(knowledge_base={"enabled": True, "kb_name": "docs"})
    result = analyzer.analyze(None, to_cfg)
    assert "knowledge_base.*" in result.matched_rules
