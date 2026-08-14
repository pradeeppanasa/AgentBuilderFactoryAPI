"""Unit tests for app.modules.guardrails.engine.GuardrailEngine
(CLAUDE_Advanced_Config.md Section 3.5 / 37.7).

FakeToxicityClassifier / FakeBedrockGuardrailClient / FailingBedrockGuardrailClient
(tests/fakes.py) stand in for the real BERT model and the real Bedrock
ApplyGuardrail API — no transformers/torch import, no AWS call.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.modules.guardrails.engine import GuardrailEngine
from app.modules.guardrails.models import GuardrailPolicy
from tests.fakes import (
    FailingBedrockGuardrailClient,
    FakeBedrockGuardrailClient,
    FakeToxicityClassifier,
)


def _policy(**overrides: object) -> GuardrailPolicy:
    now = datetime.now(UTC).isoformat()
    data: dict[str, object] = {
        "policy_id": "pol-1",
        "tenant_id": "tenant-a",
        "name": "Test Policy",
        "description": "d",
        "created_by": "tester@example.com",
        "created_at": now,
        "updated_at": now,
    }
    data.update(overrides)
    return GuardrailPolicy(**data)


def _engine(classifier_score: float, bedrock_client: object | None = None) -> GuardrailEngine:
    bedrock = bedrock_client if bedrock_client is not None else FakeBedrockGuardrailClient()
    return GuardrailEngine(
        bedrock, classifier_factory=lambda _model: FakeToxicityClassifier(classifier_score)
    )


async def test_input_blocked_by_bert_alone_never_calls_bedrock() -> None:
    bedrock = FakeBedrockGuardrailClient()
    engine = _engine(classifier_score=0.95, bedrock_client=bedrock)
    policy = _policy(bedrock_guardrail_id="gr-123")

    decision = await engine.check_input("some toxic text", policy)

    assert decision.blocked is True
    assert [layer.layer for layer in decision.layers] == ["bert"]
    assert decision.layers[0].action == "block"
    assert bedrock.calls == []


async def test_input_passed_by_bert_alone_when_clearly_safe() -> None:
    engine = _engine(classifier_score=0.05)
    policy = _policy(bedrock_guardrail_id="gr-123")

    decision = await engine.check_input("hello there", policy)

    assert decision.blocked is False
    assert [layer.action for layer in decision.layers] == ["pass"]


async def test_input_escalates_to_bedrock_when_bert_unsure_and_bedrock_passes() -> None:
    bedrock = FakeBedrockGuardrailClient(intervene=False)
    engine = _engine(classifier_score=0.5, bedrock_client=bedrock)
    policy = _policy(bedrock_guardrail_id="gr-123")

    decision = await engine.check_input("ambiguous text", policy)

    assert decision.blocked is False
    assert [layer.layer for layer in decision.layers] == ["bert", "bedrock"]
    assert decision.layers[1].action == "pass"
    assert len(bedrock.calls) == 1
    assert bedrock.calls[0]["source"] == "INPUT"


async def test_input_escalates_to_bedrock_and_bedrock_blocks() -> None:
    bedrock = FakeBedrockGuardrailClient(intervene=True)
    engine = _engine(classifier_score=0.5, bedrock_client=bedrock)
    policy = _policy(bedrock_guardrail_id="gr-123")

    decision = await engine.check_input("ambiguous text", policy)

    assert decision.blocked is True
    assert decision.layers[-1].action == "block"


async def test_input_unsure_with_no_bedrock_guardrail_configured_passes_with_note() -> None:
    engine = _engine(classifier_score=0.5)
    policy = _policy(bedrock_guardrail_id=None)

    decision = await engine.check_input("ambiguous text", policy)

    assert decision.blocked is False
    assert [layer.layer for layer in decision.layers] == ["bert", "bedrock"]
    assert decision.layers[-1].reason == "no_bedrock_guardrail_configured"


async def test_input_disabled_skips_all_layers() -> None:
    engine = _engine(classifier_score=0.99)
    policy = _policy(input_enabled=False, bedrock_guardrail_id="gr-123")

    decision = await engine.check_input("anything", policy)

    assert decision.blocked is False
    assert decision.layers == []


async def test_output_check_skips_bert_and_goes_direct_to_bedrock() -> None:
    bedrock = FakeBedrockGuardrailClient(intervene=False)
    engine = _engine(classifier_score=0.99, bedrock_client=bedrock)  # would block if BERT ran
    policy = _policy(bedrock_guardrail_id="gr-123")

    decision = await engine.check_output("some model output", policy)

    assert decision.blocked is False
    assert [layer.layer for layer in decision.layers] == ["bedrock"]
    assert bedrock.calls[0]["source"] == "OUTPUT"


async def test_output_check_returns_sanitised_text_when_redacted() -> None:
    bedrock = FakeBedrockGuardrailClient(intervene=False, sanitised_text="My name is [REDACTED]")
    engine = _engine(classifier_score=0.0, bedrock_client=bedrock)
    policy = _policy(bedrock_guardrail_id="gr-123")

    decision = await engine.check_output("My name is Alice", policy)

    assert decision.blocked is False
    assert decision.sanitised_text == "My name is [REDACTED]"


async def test_output_check_blocked_by_bedrock() -> None:
    bedrock = FakeBedrockGuardrailClient(intervene=True)
    engine = _engine(classifier_score=0.0, bedrock_client=bedrock)
    policy = _policy(bedrock_guardrail_id="gr-123")

    decision = await engine.check_output("bad output", policy)

    assert decision.blocked is True


async def test_output_disabled_skips_check() -> None:
    engine = _engine(classifier_score=0.0)
    policy = _policy(output_enabled=False, bedrock_guardrail_id="gr-123")

    decision = await engine.check_output("anything", policy)

    assert decision.blocked is False
    assert decision.layers == []


async def test_bedrock_failure_on_input_fails_closed() -> None:
    engine = _engine(classifier_score=0.5, bedrock_client=FailingBedrockGuardrailClient())
    policy = _policy(bedrock_guardrail_id="gr-123")

    decision = await engine.check_input("ambiguous text", policy)

    assert decision.blocked is True
    assert decision.layers[-1].reason == "bedrock_guardrail_unavailable"


async def test_bedrock_failure_on_output_fails_closed() -> None:
    engine = _engine(classifier_score=0.0, bedrock_client=FailingBedrockGuardrailClient())
    policy = _policy(bedrock_guardrail_id="gr-123")

    decision = await engine.check_output("anything", policy)

    assert decision.blocked is True


async def test_classifier_is_cached_per_model_name() -> None:
    calls: list[str] = []

    def factory(model_name: str) -> FakeToxicityClassifier:
        calls.append(model_name)
        return FakeToxicityClassifier(0.05)

    engine = GuardrailEngine(FakeBedrockGuardrailClient(), classifier_factory=factory)
    policy = _policy()

    await engine.check_input("hi", policy)
    await engine.check_input("hi again", policy)

    assert calls == ["unitary/toxic-bert"]


@pytest.mark.parametrize("score", [0.70, 0.71])
async def test_block_threshold_is_strictly_greater_than(score: float) -> None:
    """policy default bert_block_threshold=0.85; verifies the engine's own
    `score > threshold` (not >=) semantics using a lowered threshold."""
    engine = _engine(classifier_score=score)
    policy = _policy(bert_block_threshold=0.70, bedrock_guardrail_id="gr-123")

    decision = await engine.check_input("text", policy)

    if score > 0.70:
        assert decision.blocked is True
    else:
        assert decision.layers[0].action != "block"
