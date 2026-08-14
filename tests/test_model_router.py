"""Unit tests for app.services.model_router (CLAUDE.md Section 32.1, R27/R28/R38).

litellm.acompletion / completion_cost are monkeypatched — this suite never
makes a real model call, matching the rest of the repo's "no live AWS/network
calls" convention (moto for AWS, fakeredis for cache, and this for LiteLLM).
"""

from __future__ import annotations

from typing import Any

import litellm
import pytest

from app.modules.registry.models import AgentConfiguration
from app.services.model_router import UnsupportedModelProviderError, call_model


class _FakeMessage:
    def __init__(self, content: str) -> None:
        self.content = content


class _FakeChoice:
    def __init__(self, content: str) -> None:
        self.message = _FakeMessage(content)


class _FakeResponse:
    def __init__(self, content: str) -> None:
        self.choices = [_FakeChoice(content)]


def _config(**overrides: Any) -> AgentConfiguration:
    data: dict[str, Any] = {
        "model_id": "anthropic.claude-3-5-sonnet-20241022-v2:0",
        "model_provider": "bedrock",
        "system_prompt": "You are a helpful agent.",
    }
    data.update(overrides)
    return AgentConfiguration(**data)


async def test_call_model_returns_text_and_cost(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    async def _fake_acompletion(**kwargs: Any) -> _FakeResponse:
        captured.update(kwargs)
        return _FakeResponse("hello there")

    monkeypatch.setattr(litellm, "acompletion", _fake_acompletion)
    monkeypatch.setattr(litellm, "completion_cost", lambda completion_response: 0.0042)

    text, cost = await call_model(_config(), [{"role": "user", "content": "hi"}])

    assert text == "hello there"
    assert cost == 0.0042
    assert captured["model"] == "bedrock/anthropic.claude-3-5-sonnet-20241022-v2:0"
    assert captured["fallbacks"] is None


async def test_call_model_cost_unavailable_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _fake_acompletion(**kwargs: Any) -> _FakeResponse:
        return _FakeResponse("hi")

    def _raise_cost(completion_response: Any) -> float:
        raise RuntimeError("no pricing data for this model")

    monkeypatch.setattr(litellm, "acompletion", _fake_acompletion)
    monkeypatch.setattr(litellm, "completion_cost", _raise_cost)

    text, cost = await call_model(_config(), [{"role": "user", "content": "hi"}])

    assert text == "hi"
    assert cost is None


async def test_call_model_builds_azure_and_self_hosted_model_strings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[str] = []

    async def _fake_acompletion(**kwargs: Any) -> _FakeResponse:
        captured.append(kwargs["model"])
        return _FakeResponse("ok")

    monkeypatch.setattr(litellm, "acompletion", _fake_acompletion)
    monkeypatch.setattr(litellm, "completion_cost", lambda completion_response: None)

    await call_model(
        _config(model_provider="azure_openai", model_id="gpt-4o"),
        [{"role": "user", "content": "hi"}],
    )
    await call_model(
        _config(model_provider="self_hosted", model_id="llama-3-70b"),
        [{"role": "user", "content": "hi"}],
    )

    assert captured == ["azure/gpt-4o", "openai/llama-3-70b"]


async def test_call_model_rejects_unsupported_provider() -> None:
    with pytest.raises(UnsupportedModelProviderError):
        await call_model(
            _config(model_provider="on_prem_gpu_cluster"),
            [{"role": "user", "content": "hi"}],
        )


async def test_call_model_passes_configured_fallback_string(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    async def _fake_acompletion(**kwargs: Any) -> _FakeResponse:
        captured.update(kwargs)
        return _FakeResponse("ok")

    monkeypatch.setattr(litellm, "acompletion", _fake_acompletion)
    monkeypatch.setattr(litellm, "completion_cost", lambda completion_response: None)

    await call_model(
        _config(fallback_model_string="bedrock/anthropic.claude-3-5-haiku-20241022-v1:0"),
        [{"role": "user", "content": "hi"}],
    )

    assert captured["fallbacks"] == [
        {
            "bedrock/anthropic.claude-3-5-sonnet-20241022-v2:0": [
                "bedrock/anthropic.claude-3-5-haiku-20241022-v1:0"
            ]
        }
    ]
