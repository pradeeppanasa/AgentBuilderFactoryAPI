from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import litellm
import pytest

from llm_client import LLMClient, UnsupportedModelProviderError


class _FakeToolCall:
    def __init__(self, id: str, name: str, arguments: str) -> None:
        self.id = id
        self.function = SimpleNamespace(name=name, arguments=arguments)


def _fake_response(content: str, tool_calls: list[_FakeToolCall] | None = None) -> SimpleNamespace:
    message = SimpleNamespace(content=content, tool_calls=tool_calls)
    return SimpleNamespace(
        choices=[SimpleNamespace(message=message)],
        usage=SimpleNamespace(prompt_tokens=10, completion_tokens=5, total_tokens=15),
    )


def test_unsupported_provider_raises() -> None:
    with pytest.raises(UnsupportedModelProviderError):
        LLMClient(model_id="x", model_provider="not-a-real-provider", system_prompt="hi")


async def test_complete_builds_correct_model_string_and_prepends_system_prompt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    async def _fake_acompletion(**kwargs: Any) -> SimpleNamespace:
        captured.update(kwargs)
        return _fake_response("Hello there.")

    monkeypatch.setattr(litellm, "acompletion", _fake_acompletion)

    client = LLMClient(
        model_id="anthropic.claude-3-5-sonnet-20241022-v2:0",
        model_provider="bedrock",
        system_prompt="You are a FAQ agent.",
        temperature=0.2,
        max_tokens=1024,
    )
    result = await client.complete(messages=[{"role": "user", "content": "Hi"}])

    assert captured["model"] == "bedrock/anthropic.claude-3-5-sonnet-20241022-v2:0"
    assert captured["messages"][0] == {"role": "system", "content": "You are a FAQ agent."}
    assert captured["messages"][1] == {"role": "user", "content": "Hi"}
    assert captured["temperature"] == 0.2
    assert captured["max_tokens"] == 1024
    assert captured["fallbacks"] is None
    assert result.content == "Hello there."
    assert result.input_tokens == 10
    assert result.output_tokens == 5


async def test_fallback_model_string_sets_litellm_fallbacks(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    async def _fake_acompletion(**kwargs: Any) -> SimpleNamespace:
        captured.update(kwargs)
        return _fake_response("ok")

    monkeypatch.setattr(litellm, "acompletion", _fake_acompletion)

    client = LLMClient(
        model_id="gpt-4o",
        model_provider="azure_openai",
        system_prompt="sys",
        fallback_model_string="bedrock/anthropic.claude-3-5-haiku-20241022-v1:0",
    )
    await client.complete(messages=[{"role": "user", "content": "hi"}])

    assert captured["fallbacks"] == [
        {"azure/gpt-4o": ["bedrock/anthropic.claude-3-5-haiku-20241022-v1:0"]}
    ]


async def test_complete_extracts_tool_calls(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _fake_acompletion(**kwargs: Any) -> SimpleNamespace:
        return _fake_response(
            "", tool_calls=[_FakeToolCall("call-1", "companies-house", '{"company_number": "123"}')]
        )

    monkeypatch.setattr(litellm, "acompletion", _fake_acompletion)

    client = LLMClient(model_id="m", model_provider="bedrock", system_prompt="sys")
    result = await client.complete(messages=[{"role": "user", "content": "hi"}])

    assert result.tool_calls == [
        {"id": "call-1", "name": "companies-house", "arguments": '{"company_number": "123"}'}
    ]


async def test_complete_survives_completion_cost_raising(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _fake_acompletion(**kwargs: Any) -> SimpleNamespace:
        return _fake_response("ok")

    def _raising_cost(**kwargs: Any) -> float:
        raise RuntimeError("no pricing data for this model")

    monkeypatch.setattr(litellm, "acompletion", _fake_acompletion)
    monkeypatch.setattr(litellm, "completion_cost", _raising_cost)

    client = LLMClient(model_id="m", model_provider="bedrock", system_prompt="sys")
    result = await client.complete(messages=[{"role": "user", "content": "hi"}])

    assert result.cost_usd is None
    assert result.content == "ok"
