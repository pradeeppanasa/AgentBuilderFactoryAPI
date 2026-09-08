from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest
import tool_executor as tool_executor_module
import tool_policy as tool_policy_module
from tool_executor import ToolExecutor, tool_lambda_name

from fakes import FakeLambdaClient

_MAIN_REPO_ROOT = Path(__file__).resolve().parents[3]

# Sprint 3 Phase 4 (R64) default-deny — any test that expects a tool call
# to actually reach the Lambda now needs an explicit policy allowing it.
_ALLOW_COMPANIES_HOUSE = [{"tool": "companies-house", "allowed": True, "risk": "LOW"}]


@pytest.fixture(autouse=True)
def _no_real_audit_writes(monkeypatch: pytest.MonkeyPatch) -> None:
    """Sprint 3 Phase 2 wired a real write_audit_event("tool.invoked") call
    into execute() — never raises, but a real one would attempt a genuine
    (slow, credential-less) AWS call in every test here. tool_policy.py
    (Phase 4) imports write_audit_event separately from tool_executor.py —
    both bindings need patching, not just tool_executor's own."""
    monkeypatch.setattr(tool_executor_module, "write_audit_event", lambda **_kwargs: None)
    monkeypatch.setattr(tool_policy_module, "write_audit_event", lambda **_kwargs: None)


@pytest.mark.parametrize(
    ("agent_id", "tool_id"),
    [
        ("faq-agent-9046a4", "companies-house"),
        ("kyc-document-verification-agent-3181e1", "companies-house"),
        ("kyc-document-verification-agent-3181e1", "salesforce-crm-lookup"),
    ],
)
def test_lambda_name_matches_terraform_naming_convention(agent_id: str, tool_id: str) -> None:
    """This truncation logic is intentionally duplicated from
    app/modules/iac_generator/naming.py's tool_lambda_name() (F8: separate
    deployable, zero shared code) — this test is what actually catches the
    two silently drifting apart, by importing the real one and comparing."""
    sys.path.insert(0, str(_MAIN_REPO_ROOT))
    try:
        from app.modules.iac_generator.naming import tool_lambda_name as real_tool_lambda_name
    finally:
        sys.path.remove(str(_MAIN_REPO_ROOT))

    assert tool_lambda_name(agent_id, tool_id) == real_tool_lambda_name(agent_id, tool_id)


def test_lambda_name_never_exceeds_64_chars() -> None:
    name = tool_lambda_name("a" * 80, "b" * 40)
    assert len(name) <= 64


def test_get_definitions_returns_openai_function_shape() -> None:
    executor = ToolExecutor(
        agent_id="faq-agent-1",
        tenant_id="tenant-a",
        tools=[
            {
                "tool_id": "companies-house",
                "tool_name": "Companies House Lookup",
                "input_schema": {
                    "type": "object",
                    "properties": {"company_number": {"type": "string"}},
                },
            }
        ],
        lambda_client=FakeLambdaClient({}),
    )

    definitions = executor.get_definitions()

    assert definitions == [
        {
            "type": "function",
            "function": {
                "name": "companies-house",
                "description": "Companies House Lookup",
                "parameters": {
                    "type": "object",
                    "properties": {"company_number": {"type": "string"}},
                },
            },
        }
    ]


async def test_execute_invokes_the_correct_lambda_with_parsed_arguments() -> None:
    fake_lambda = FakeLambdaClient({"panasa-faq-agent-1-tool-companies-house": {"status": "found"}})
    executor = ToolExecutor(
        agent_id="faq-agent-1",
        tenant_id="tenant-a",
        tools=[{"tool_id": "companies-house", "tool_name": "Companies House"}],
        tool_policies=_ALLOW_COMPANIES_HOUSE,
        lambda_client=fake_lambda,
    )

    results = await executor.execute(
        [
            {
                "id": "call-1",
                "name": "companies-house",
                "arguments": json.dumps({"company_number": "123"}),
            }
        ]
    )

    assert results == [{"tool_id": "companies-house", "result": {"status": "found"}}]
    assert fake_lambda.calls[0]["FunctionName"] == "panasa-faq-agent-1-tool-companies-house"
    assert json.loads(fake_lambda.calls[0]["Payload"]) == {"company_number": "123"}


async def test_execute_writes_tool_invoked_audit_event_on_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[dict[str, Any]] = []
    monkeypatch.setattr(
        tool_executor_module, "write_audit_event", lambda **kwargs: events.append(kwargs)
    )
    fake_lambda = FakeLambdaClient({"panasa-faq-agent-1-tool-companies-house": {"status": "found"}})
    executor = ToolExecutor(
        agent_id="faq-agent-1",
        tenant_id="tenant-a",
        tools=[{"tool_id": "companies-house", "tool_name": "Companies House"}],
        tool_policies=_ALLOW_COMPANIES_HOUSE,
        lambda_client=fake_lambda,
    )

    await executor.execute(
        [
            {
                "id": "call-1",
                "name": "companies-house",
                "arguments": json.dumps({"company_number": "123"}),
            }
        ]
    )

    assert len(events) == 1
    assert events[0]["event_type"] == "tool.invoked"
    assert events[0]["tenant_id"] == "tenant-a"
    assert events[0]["agent_id"] == "faq-agent-1"
    assert events[0]["resource"] == "companies-house"
    assert events[0]["result"] == "success"


async def test_execute_writes_tool_invoked_audit_event_on_lambda_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[dict[str, Any]] = []
    monkeypatch.setattr(
        tool_executor_module, "write_audit_event", lambda **kwargs: events.append(kwargs)
    )

    class _FailingLambdaClient:
        def invoke(self, **kwargs: Any) -> Any:
            class _Payload:
                def read(self) -> bytes:
                    return json.dumps({"errorMessage": "boom"}).encode("utf-8")

            return {"Payload": _Payload(), "FunctionError": "Unhandled"}

    executor = ToolExecutor(
        agent_id="faq-agent-1",
        tenant_id="tenant-a",
        tools=[{"tool_id": "companies-house", "tool_name": "Companies House"}],
        tool_policies=_ALLOW_COMPANIES_HOUSE,
        lambda_client=_FailingLambdaClient(),
    )

    await executor.execute([{"id": "call-1", "name": "companies-house", "arguments": "{}"}])

    # Sprint 4 Phase 2 (S-05, CLAUDE.md Section 61.2) — tool.invoked records
    # every attempt regardless of outcome; tool.failed is written alongside
    # it (not instead of it) specifically for the Lambda-returned-error case.
    assert len(events) == 2
    assert events[0]["event_type"] == "tool.invoked"
    assert events[0]["result"] == "error"
    assert events[1]["event_type"] == "tool.failed"
    assert events[1]["result"] == "error"


async def test_execute_raises_tool_denied_when_tool_not_in_policy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Sprint 3 Phase 4 (R64) default-deny — a tool present in `tools` but
    absent from tool_policies must still be denied, never silently allowed
    just because a Lambda exists for it."""
    from tool_policy import ToolDeniedError

    fake_lambda = FakeLambdaClient({"panasa-faq-agent-1-tool-companies-house": {"status": "found"}})
    executor = ToolExecutor(
        agent_id="faq-agent-1",
        tenant_id="tenant-a",
        tools=[{"tool_id": "companies-house", "tool_name": "Companies House"}],
        tool_policies=[],  # nothing allowed
        lambda_client=fake_lambda,
    )

    with pytest.raises(ToolDeniedError) as excinfo:
        await executor.execute([{"id": "call-1", "name": "companies-house", "arguments": "{}"}])

    assert "default deny" in str(excinfo.value)
    assert fake_lambda.calls == []


async def test_execute_raises_approval_required_for_high_risk_tool() -> None:
    from tool_policy import ApprovalRequiredError

    fake_lambda = FakeLambdaClient({"panasa-faq-agent-1-tool-db-delete": {"ok": True}})
    executor = ToolExecutor(
        agent_id="faq-agent-1",
        tenant_id="tenant-a",
        tools=[{"tool_id": "db-delete", "tool_name": "Delete Record"}],
        tool_policies=[{"tool": "db-delete", "allowed": True, "risk": "HIGH"}],
        lambda_client=fake_lambda,
    )

    with pytest.raises(ApprovalRequiredError):
        await executor.execute([{"id": "call-1", "name": "db-delete", "arguments": "{}"}])

    assert fake_lambda.calls == []


async def test_execute_allows_medium_risk_tool_without_approval() -> None:
    fake_lambda = FakeLambdaClient({"panasa-faq-agent-1-tool-jira-create": {"issue": "PROJ-1"}})
    executor = ToolExecutor(
        agent_id="faq-agent-1",
        tenant_id="tenant-a",
        tools=[{"tool_id": "jira-create", "tool_name": "Create Jira Issue"}],
        tool_policies=[{"tool": "jira-create", "allowed": True, "risk": "MEDIUM"}],
        lambda_client=fake_lambda,
    )

    results = await executor.execute([{"id": "call-1", "name": "jira-create", "arguments": "{}"}])

    assert results == [{"tool_id": "jira-create", "result": {"issue": "PROJ-1"}}]
    assert len(fake_lambda.calls) == 1


async def test_execute_reports_unknown_tool_without_calling_lambda() -> None:
    fake_lambda = FakeLambdaClient({})
    executor = ToolExecutor(
        agent_id="faq-agent-1", tenant_id="tenant-a", tools=[], lambda_client=fake_lambda
    )

    results = await executor.execute(
        [{"id": "call-1", "name": "does-not-exist", "arguments": "{}"}]
    )

    assert results == [{"tool_id": "does-not-exist", "error": "unknown tool"}]
    assert fake_lambda.calls == []


async def test_execute_reports_invalid_json_arguments() -> None:
    fake_lambda = FakeLambdaClient({})
    executor = ToolExecutor(
        agent_id="faq-agent-1",
        tenant_id="tenant-a",
        tools=[{"tool_id": "companies-house", "tool_name": "Companies House"}],
        lambda_client=fake_lambda,
    )

    results = await executor.execute(
        [{"id": "call-1", "name": "companies-house", "arguments": "{not-json"}]
    )

    assert len(results) == 1
    assert results[0]["tool_id"] == "companies-house"
    assert "invalid arguments" in results[0]["error"]
    assert fake_lambda.calls == []
