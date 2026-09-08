"""Unit tests for the Sprint 3 Phase 4 Tool Policy Engine (CLAUDE.md
Section 64.3, S-08, R64)."""

from __future__ import annotations

from typing import Any

import pytest
import tool_policy as tool_policy_module
from tool_policy import ApprovalRequiredError, RiskLevel, ToolDeniedError, ToolPolicyEngine


@pytest.fixture(autouse=True)
def _capture_audit_events(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    monkeypatch.setattr(
        tool_policy_module, "write_audit_event", lambda **kwargs: events.append(kwargs)
    )
    return events


def test_check_denies_tool_absent_from_policy() -> None:
    engine = ToolPolicyEngine([])

    decision = engine.check("db-delete")

    assert decision.allowed is False
    assert decision.requires_approval is False
    assert "default deny" in decision.reason


def test_check_denies_tool_explicitly_disallowed() -> None:
    engine = ToolPolicyEngine([{"tool": "db-delete", "allowed": False, "risk": "LOW"}])

    decision = engine.check("db-delete")

    assert decision.allowed is False
    assert "disabled" in decision.reason


@pytest.mark.parametrize("risk", ["LOW", "MEDIUM"])
def test_check_allows_low_and_medium_risk_without_approval(risk: str) -> None:
    engine = ToolPolicyEngine([{"tool": "jira-search", "allowed": True, "risk": risk}])

    decision = engine.check("jira-search")

    assert decision.allowed is True
    assert decision.requires_approval is False


@pytest.mark.parametrize("risk", ["HIGH", "DESTRUCTIVE"])
def test_check_requires_approval_for_high_and_destructive_risk(risk: str) -> None:
    engine = ToolPolicyEngine([{"tool": "payment-transfer", "allowed": True, "risk": risk}])

    decision = engine.check("payment-transfer")

    assert decision.allowed is True
    assert decision.requires_approval is True


def test_check_defaults_to_low_risk_when_risk_field_omitted() -> None:
    engine = ToolPolicyEngine([{"tool": "jira-search", "allowed": True}])

    decision = engine.check("jira-search")

    assert decision.requires_approval is False


def test_enforce_raises_tool_denied_and_writes_tool_denied_event(
    _capture_audit_events: list[dict[str, Any]],
) -> None:
    engine = ToolPolicyEngine([])

    with pytest.raises(ToolDeniedError) as excinfo:
        engine.enforce("db-delete", tenant_id="tenant-a", agent_id="agent-1", principal_id="p1")

    assert excinfo.value.tool_id == "db-delete"
    assert len(_capture_audit_events) == 1
    assert _capture_audit_events[0]["event_type"] == "tool.denied"
    assert _capture_audit_events[0]["result"] == "denied"
    assert _capture_audit_events[0]["tenant_id"] == "tenant-a"


def test_enforce_raises_approval_required_and_writes_human_approval_requested_event(
    _capture_audit_events: list[dict[str, Any]],
) -> None:
    engine = ToolPolicyEngine(
        [{"tool": "payment-transfer", "allowed": True, "risk": "DESTRUCTIVE"}]
    )

    with pytest.raises(ApprovalRequiredError) as excinfo:
        engine.enforce(
            "payment-transfer", tenant_id="tenant-a", agent_id="agent-1", principal_id="p1"
        )

    assert excinfo.value.tool_id == "payment-transfer"
    assert len(_capture_audit_events) == 1
    assert _capture_audit_events[0]["event_type"] == "human.approval.requested"
    assert _capture_audit_events[0]["result"] == "pending"


def test_enforce_does_not_raise_or_write_any_event_for_allowed_low_risk_tool(
    _capture_audit_events: list[dict[str, Any]],
) -> None:
    engine = ToolPolicyEngine([{"tool": "jira-search", "allowed": True, "risk": "LOW"}])

    engine.enforce("jira-search", tenant_id="tenant-a", agent_id="agent-1", principal_id="p1")

    assert _capture_audit_events == []


def test_risk_level_enum_has_exactly_four_values() -> None:
    assert {r.value for r in RiskLevel} == {"LOW", "MEDIUM", "HIGH", "DESTRUCTIVE"}
