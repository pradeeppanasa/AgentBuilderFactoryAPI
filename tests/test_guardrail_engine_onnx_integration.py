"""End-to-end integration test: GuardrailEngine wired to the REAL
ONNXBertClassifier (not FakeToxicityClassifier) — verifies the full Layer 1
-> Layer 2 flow still holds after replacing the transformers/torch
implementation with ONNX Runtime:

    Input
       |
    ONNX/BERT Layer 1
       |
    Clearly safe -> continue (no Bedrock call)
       |
    Unsure -> Bedrock Layer 2
       |
    Decision

test_guardrail_engine.py covers this same flow against FakeToxicityClassifier
(the classifier's own correctness isn't its concern); this file is the one
place both real halves — the real ONNX classifier and the engine's routing
logic — run together.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.config import settings
from app.modules.guardrails.bert_classifier import ONNXBertClassifier
from app.modules.guardrails.engine import GuardrailEngine
from app.modules.guardrails.models import GuardrailPolicy
from tests.fakes import FakeBedrockGuardrailClient
from tests.guardrail_onnx_fixtures import build_synthetic_model_dir


@pytest.fixture
def real_onnx_model(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    build_synthetic_model_dir(tmp_path)
    monkeypatch.setattr(settings, "guardrails_bert_model_dir", str(tmp_path))


def _policy(**overrides: object) -> GuardrailPolicy:
    from datetime import UTC, datetime

    now = datetime.now(UTC).isoformat()
    data: dict[str, object] = {
        "policy_id": "pol-onnx-1",
        "tenant_id": "tenant-a",
        "name": "ONNX Integration Policy",
        "description": "d",
        "created_by": "tester@example.com",
        "created_at": now,
        "updated_at": now,
    }
    data.update(overrides)
    return GuardrailPolicy(**data)


async def test_engine_with_real_onnx_classifier_passes_clearly_safe_input(
    real_onnx_model: None,
) -> None:
    bedrock = FakeBedrockGuardrailClient()
    engine = GuardrailEngine(bedrock, classifier_factory=ONNXBertClassifier)
    policy = _policy(bedrock_guardrail_id="gr-123")

    decision = await engine.check_input("this is a safe message", policy)

    assert decision.blocked is False
    assert [layer.action for layer in decision.layers] == ["pass"]
    assert bedrock.calls == []  # BERT alone was confident enough — no Layer 2 call


async def test_engine_with_real_onnx_classifier_blocks_clearly_toxic_input(
    real_onnx_model: None,
) -> None:
    bedrock = FakeBedrockGuardrailClient()
    engine = GuardrailEngine(bedrock, classifier_factory=ONNXBertClassifier)
    policy = _policy(bedrock_guardrail_id="gr-123")

    decision = await engine.check_input("this is a toxic message", policy)

    assert decision.blocked is True
    assert decision.layers[0].layer == "bert"
    assert decision.layers[0].action == "block"
    assert bedrock.calls == []  # blocked at Layer 1 — never reaches Layer 2


async def test_engine_with_real_onnx_classifier_escalates_unsure_input_to_bedrock(
    real_onnx_model: None,
) -> None:
    bedrock = FakeBedrockGuardrailClient(intervene=False)
    engine = GuardrailEngine(bedrock, classifier_factory=ONNXBertClassifier)
    policy = _policy(bedrock_guardrail_id="gr-123")

    decision = await engine.check_input("this is an unsure message", policy)

    assert decision.blocked is False
    assert [layer.layer for layer in decision.layers] == ["bert", "bedrock"]
    assert decision.layers[0].action == "escalate"
    assert len(bedrock.calls) == 1
    assert bedrock.calls[0]["source"] == "INPUT"


async def test_engine_with_real_onnx_classifier_unsure_input_blocked_by_bedrock(
    real_onnx_model: None,
) -> None:
    bedrock = FakeBedrockGuardrailClient(intervene=True)
    engine = GuardrailEngine(bedrock, classifier_factory=ONNXBertClassifier)
    policy = _policy(bedrock_guardrail_id="gr-123")

    decision = await engine.check_input("this is an unsure message", policy)

    assert decision.blocked is True
    assert decision.layers[-1].layer == "bedrock"
    assert decision.layers[-1].action == "block"
