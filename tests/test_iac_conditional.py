"""Unit tests for resolve_required_modules (CLAUDE.md Section 8 / R20).

Pure unit tests against AgentConfiguration — no DB, no HTTP, no AWS.
"""

from typing import Any

from app.modules.iac_generator.conditional import resolve_required_modules
from app.modules.registry.models import AgentConfiguration

_ALWAYS_ON = {"base", "api_gateway", "authentication", "compute", "observability"}


def _config(**overrides: Any) -> AgentConfiguration:
    data: dict[str, Any] = {
        "model_id": "anthropic.claude-3-5-sonnet-20241022-v2:0",
        "model_provider": "bedrock",
        "system_prompt": "You are a helpful agent.",
    }
    data.update(overrides)
    return AgentConfiguration(**data)


def test_minimal_agent_gets_always_on_modules_plus_defaults() -> None:
    # GuardrailConfig defaults all three flags True, and audit_enabled
    # defaults True — both modules are included unless explicitly disabled.
    modules = resolve_required_modules(_config())
    assert set(modules) == _ALWAYS_ON | {"guardrails", "audit"}


def test_fully_stripped_agent_gets_only_always_on_modules() -> None:
    modules = resolve_required_modules(
        _config(
            guardrails={
                "prompt_injection": False,
                "pii_detection": False,
                "toxicity_filter": False,
            },
            audit_enabled=False,
        )
    )
    assert set(modules) == _ALWAYS_ON


def test_no_knowledge_base_means_no_rag_module() -> None:
    modules = resolve_required_modules(_config())
    assert "rag" not in modules


def test_knowledge_base_enabled_adds_rag_module() -> None:
    modules = resolve_required_modules(_config(knowledge_base={"enabled": True, "kb_name": "docs"}))
    assert "rag" in modules


def test_knowledge_base_present_but_disabled_does_not_add_rag_module() -> None:
    modules = resolve_required_modules(
        _config(knowledge_base={"enabled": False, "kb_name": "docs"})
    )
    assert "rag" not in modules


def test_no_tools_means_no_tools_module() -> None:
    modules = resolve_required_modules(_config(tools=[]))
    assert "tools" not in modules


def test_tools_configured_adds_tools_module() -> None:
    modules = resolve_required_modules(
        _config(
            tools=[
                {
                    "tool_id": "jira",
                    "tool_name": "Jira",
                    "executor_type": "http",
                    "input_schema": {},
                }
            ]
        )
    )
    assert "tools" in modules


def test_ts01_a01_simple_agent_with_one_tool_gets_seven_modules_not_orchestrator() -> None:
    """TS01-A-01 regression. The bug report's literal expectation was 6
    modules (no compute/ECS module at all) — that would leave the agent
    with nowhere to actually run; the real fix (confirmed with the user)
    is renaming the always-on "orchestrator" module to "compute", not
    removing it. A standard agent with one tool and guardrails off — the
    exact KYC Document Verification Agent repro from the bug report — gets
    7 modules including "compute"; "orchestrator" must never appear as a
    module name again, for any agent_type."""
    modules = resolve_required_modules(
        _config(
            guardrails={
                "prompt_injection": False,
                "pii_detection": False,
                "toxicity_filter": False,
            },
            tools=[
                {
                    "tool_id": "companies-house",
                    "tool_name": "Companies House Lookup",
                    "executor_type": "http",
                    "input_schema": {},
                }
            ],
        )
    )
    assert set(modules) == _ALWAYS_ON | {"tools", "audit"}
    assert len(modules) == 7
    assert "compute" in modules
    assert "orchestrator" not in modules


def test_guardrails_all_disabled_means_no_guardrails_module() -> None:
    modules = resolve_required_modules(
        _config(
            guardrails={
                "prompt_injection": False,
                "pii_detection": False,
                "toxicity_filter": False,
            }
        )
    )
    assert "guardrails" not in modules


def test_any_guardrail_enabled_adds_guardrails_module() -> None:
    modules = resolve_required_modules(
        _config(
            guardrails={
                "prompt_injection": False,
                "pii_detection": True,
                "toxicity_filter": False,
            }
        )
    )
    assert "guardrails" in modules


def test_human_review_disabled_means_no_human_loop_module() -> None:
    modules = resolve_required_modules(_config(human_review={"enabled": False}))
    assert "human_loop" not in modules


def test_human_review_enabled_adds_human_loop_module() -> None:
    modules = resolve_required_modules(
        _config(human_review={"enabled": True, "trigger_conditions": ["high_risk_decision"]})
    )
    assert "human_loop" in modules


def test_audit_disabled_means_no_audit_module() -> None:
    modules = resolve_required_modules(_config(audit_enabled=False))
    assert "audit" not in modules


def test_everything_enabled_includes_every_module() -> None:
    modules = resolve_required_modules(
        _config(
            knowledge_base={"enabled": True, "kb_name": "docs"},
            tools=[
                {
                    "tool_id": "jira",
                    "tool_name": "Jira",
                    "executor_type": "http",
                    "input_schema": {},
                }
            ],
            human_review={"enabled": True, "trigger_conditions": ["high_risk_decision"]},
            audit_enabled=True,
        )
    )
    assert set(modules) == _ALWAYS_ON | {"guardrails", "rag", "tools", "human_loop", "audit"}
