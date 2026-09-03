from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from fakes import FakeLambdaClient

from tool_executor import ToolExecutor, tool_lambda_name

_MAIN_REPO_ROOT = Path(__file__).resolve().parents[3]


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
        tools=[
            {
                "tool_id": "companies-house",
                "tool_name": "Companies House Lookup",
                "input_schema": {"type": "object", "properties": {"company_number": {"type": "string"}}},
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
        tools=[{"tool_id": "companies-house", "tool_name": "Companies House"}],
        lambda_client=fake_lambda,
    )

    results = await executor.execute(
        [{"id": "call-1", "name": "companies-house", "arguments": json.dumps({"company_number": "123"})}]
    )

    assert results == [{"tool_id": "companies-house", "result": {"status": "found"}}]
    assert fake_lambda.calls[0]["FunctionName"] == "panasa-faq-agent-1-tool-companies-house"
    assert json.loads(fake_lambda.calls[0]["Payload"]) == {"company_number": "123"}


async def test_execute_reports_unknown_tool_without_calling_lambda() -> None:
    fake_lambda = FakeLambdaClient({})
    executor = ToolExecutor(agent_id="faq-agent-1", tools=[], lambda_client=fake_lambda)

    results = await executor.execute([{"id": "call-1", "name": "does-not-exist", "arguments": "{}"}])

    assert results == [{"tool_id": "does-not-exist", "error": "unknown tool"}]
    assert fake_lambda.calls == []


async def test_execute_reports_invalid_json_arguments() -> None:
    fake_lambda = FakeLambdaClient({})
    executor = ToolExecutor(
        agent_id="faq-agent-1",
        tools=[{"tool_id": "companies-house", "tool_name": "Companies House"}],
        lambda_client=fake_lambda,
    )

    results = await executor.execute([{"id": "call-1", "name": "companies-house", "arguments": "{not-json"}])

    assert len(results) == 1
    assert results[0]["tool_id"] == "companies-house"
    assert "invalid arguments" in results[0]["error"]
    assert fake_lambda.calls == []
