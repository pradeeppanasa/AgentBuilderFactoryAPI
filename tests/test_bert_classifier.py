"""Real ONNX Runtime inference tests for
app.modules.guardrails.bert_classifier.ONNXBertClassifier
(CLAUDE_Advanced_Config.md Section 3.5 Layer 1).

Runs actual onnxruntime inference against a small, real, synthetic ONNX
graph (tests/guardrail_onnx_fixtures.py) — not a fake/stub classifier, and
not a real unitary/toxic-bert download (which would itself violate the
"never fetch models at runtime" rule this module enforces in production).
Only the model *weights* are synthetic; the tokenizer, ONNX graph, and
inference session are all the real onnxruntime/tokenizers code paths.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.config import settings
from app.modules.guardrails.bert_classifier import (
    GuardrailInferenceError,
    GuardrailModelUnavailableError,
    ONNXBertClassifier,
    _is_toxic_label,
)
from tests.guardrail_onnx_fixtures import MODEL_NAME, build_synthetic_model_dir


@pytest.mark.parametrize(
    ("label", "expected"),
    [
        ("toxic", True),
        ("severe_toxic", True),
        ("TOXICITY", True),
        ("non_toxic", False),
        ("non-toxic", False),
        ("nontoxic", False),
        ("not_toxic", False),
        ("NOT-TOXIC", False),
        ("insult", False),
        ("obscene", False),
    ],
)
def test_is_toxic_label_excludes_negated_labels(label: str, expected: bool) -> None:
    assert _is_toxic_label(label) is expected


@pytest.fixture
def model_base_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    build_synthetic_model_dir(tmp_path)
    monkeypatch.setattr(settings, "guardrails_bert_model_dir", str(tmp_path))
    return tmp_path


def test_scores_clearly_safe_input(model_base_dir: Path) -> None:
    classifier = ONNXBertClassifier(MODEL_NAME)

    score = classifier.score("this is a safe message")

    assert score < 0.1


def test_scores_clearly_toxic_input(model_base_dir: Path) -> None:
    classifier = ONNXBertClassifier(MODEL_NAME)

    score = classifier.score("this is a toxic message")

    assert score > 0.9


def test_scores_unsure_threshold_case(model_base_dir: Path) -> None:
    classifier = ONNXBertClassifier(MODEL_NAME)

    score = classifier.score("this is an unsure message")

    assert 0.4 < score < 0.6


def test_empty_string_scores_as_safe_not_a_crash(model_base_dir: Path) -> None:
    """Regression: a zero-token sequence (no [CLS]/[SEP] wrapping) crashes
    onnxruntime's ReduceMax node outright — verified while building the
    test fixture. The real tokenizer template always emits [CLS] ... [SEP],
    so an empty string still produces a valid 2-token sequence."""
    classifier = ONNXBertClassifier(MODEL_NAME)

    score = classifier.score("")

    assert score < 0.1


def test_session_and_tokenizer_loaded_once_and_cached(model_base_dir: Path) -> None:
    classifier = ONNXBertClassifier(MODEL_NAME)

    classifier.score("first call")
    session_after_first = classifier._session
    tokenizer_after_first = classifier._tokenizer

    classifier.score("second call")

    assert classifier._session is session_after_first
    assert classifier._tokenizer is tokenizer_after_first


@pytest.mark.parametrize("bad_input", [None, 123, [], {"text": "hi"}])
def test_malformed_input_raises_type_error(model_base_dir: Path, bad_input: object) -> None:
    classifier = ONNXBertClassifier(MODEL_NAME)

    with pytest.raises(TypeError):
        classifier.score(bad_input)  # type: ignore[arg-type]


def test_model_unavailable_when_dir_not_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "guardrails_bert_model_dir", None)
    classifier = ONNXBertClassifier(MODEL_NAME)

    with pytest.raises(GuardrailModelUnavailableError):
        classifier.score("hello")


def test_model_unavailable_when_model_files_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "guardrails_bert_model_dir", str(tmp_path))
    classifier = ONNXBertClassifier("some/other-model")

    with pytest.raises(GuardrailModelUnavailableError):
        classifier.score("hello")


def test_model_unavailable_when_model_file_corrupt(model_base_dir: Path) -> None:
    model_path = model_base_dir / MODEL_NAME / "model.onnx"
    model_path.write_bytes(b"not a valid onnx file at all")
    classifier = ONNXBertClassifier(MODEL_NAME)

    with pytest.raises(GuardrailModelUnavailableError):
        classifier.score("hello")


def test_model_unavailable_rejects_path_traversal_in_model_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "guardrails_bert_model_dir", str(tmp_path))
    classifier = ONNXBertClassifier("../../etc")

    with pytest.raises(GuardrailModelUnavailableError):
        classifier.score("hello")


def test_inference_failure_after_successful_load_raises_clean_error(
    model_base_dir: Path,
) -> None:
    classifier = ONNXBertClassifier(MODEL_NAME)
    classifier.score("warm up the session")  # forces a successful load first

    def _broken_run(*args: object, **kwargs: object) -> None:
        raise RuntimeError("simulated ONNX Runtime crash")

    classifier._session.run = _broken_run  # type: ignore[method-assign]

    with pytest.raises(GuardrailInferenceError):
        classifier.score("this should fail during inference, not loading")


def test_fallback_without_config_json_still_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No config.json -> _resolve_toxic_label_indices falls back to every
    output index (max-of-all) rather than crashing. This is a degraded mode
    (it can't identify which logit is "toxic"), which is exactly why
    production model artifacts must ship config.json alongside model.onnx —
    this test only asserts the fallback path executes and returns a valid
    probability, not that it picks the "correct" direction."""
    model_dir = build_synthetic_model_dir(tmp_path)
    (model_dir / "config.json").unlink()
    monkeypatch.setattr(settings, "guardrails_bert_model_dir", str(tmp_path))
    classifier = ONNXBertClassifier(MODEL_NAME)

    score = classifier.score("this is a toxic message")

    assert 0.0 <= score <= 1.0
