"""ONNX Runtime toxicity classifier — Guardrail Layer 1
(CLAUDE_Advanced_Config.md Section 3.5 Layer 1 — "local inference (runs
inside VPC — R30)").

Deliberately NOT PyTorch/`optimum`: every version of `optimum` (including
its "onnxruntime" extra) lists `torch>=1.11` as a mandatory, non-optional
dependency in its own package metadata (confirmed 2026-08-15 by inspecting
the wheel METADATA directly) — there is no way to install it without
PyTorch, which conflicts with "no PyTorch in the production runtime".
`onnxruntime` (a pure C++ inference engine) + `tokenizers` (Hugging Face's
standalone Rust tokenizer library) together load and run a pre-exported
ONNX model with zero PyTorch/TensorFlow anywhere in the dependency tree.

All three heavy imports (onnxruntime, tokenizers, numpy) are deferred to
first *use* (`_ensure_loaded`), not module import or even `ONNXBertClassifier`
construction — this is what lets every other module (including
app.modules.guardrails.engine, and therefore anything that imports the
guardrail engine transitively) import cleanly in an environment where these
packages aren't installed. Only actually scoring text requires them.

No network access, ever: the model artifact
(`{model.onnx, tokenizer.json, config.json}`) is loaded from a local
directory (`{settings.guardrails_bert_model_dir}/{model_name}/`) supplied
at image build time or mounted from a customer-side artifact location.
This module never calls out to Panasa or the Hugging Face Hub at runtime
— no auto-download, matching the "no runtime dependency on Panasa" rule
applied elsewhere in this codebase (R03/R04).

Tests exercise this against a small, real, synthetic ONNX graph + tokenizer
(tests/guardrail_onnx_fixtures.py) — real onnxruntime inference, not a real
unitary/toxic-bert model (fetching real model weights at test time would
itself violate the no-auto-download rule this module enforces at runtime).
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Protocol

from app.config import settings
from app.shared.logging import get_logger

log = get_logger()

# Matches "toxic", "severe_toxic", "toxicity", etc. but deliberately
# excludes "non_toxic" / "non-toxic" / "nontoxic" / "not_toxic" — a naive
# substring check ("toxic" in label) also matches those negated label
# names, since "toxic" is literally a substring of "non_toxic". Caught by
# tests/test_bert_classifier.py's real ONNX inference test against a
# synthetic {"0": "non_toxic", "1": "toxic"} config — a fake/fixed-score
# classifier double would never have exercised this matching logic at all.
_NEGATED_TOXIC = re.compile(r"(?:non|not)[-_]?toxic", re.IGNORECASE)


def _is_toxic_label(label: str) -> bool:
    return "toxic" in label.lower() and not _NEGATED_TOXIC.search(label)


class ToxicityClassifier(Protocol):
    def score(self, text: str) -> float:
        """Returns a toxicity confidence score in [0.0, 1.0]."""
        ...


class GuardrailModelUnavailableError(RuntimeError):
    """Raised when the ONNX model/tokenizer artifact isn't configured,
    doesn't exist on disk, or fails to load (corrupt/invalid files) — any
    fault that occurs before a single token is scored."""


class GuardrailInferenceError(RuntimeError):
    """Raised when tokenization or the ONNX Runtime forward pass itself
    fails after the model already loaded successfully — a runtime
    inference fault, distinct from a load-time GuardrailModelUnavailableError."""


class ONNXBertClassifier:
    """Loads `{base_dir}/{model_name}/{model.onnx, tokenizer.json,
    config.json}` and scores text via a real ONNX Runtime session."""

    def __init__(self, model_name: str) -> None:
        self._model_name = model_name
        self._session: Any = None
        self._tokenizer: Any = None
        self._np: Any = None
        self._toxic_label_indices: list[int] = []

    def _model_dir(self) -> Path:
        base_dir = settings.guardrails_bert_model_dir
        if not base_dir:
            raise GuardrailModelUnavailableError(
                "GUARDRAILS_BERT_MODEL_DIR is not configured — no local ONNX "
                "model artifact is available for Guardrail Layer 1."
            )
        if ".." in Path(self._model_name).parts:
            raise GuardrailModelUnavailableError(
                f"Refusing to resolve model_name {self._model_name!r} outside "
                f"GUARDRAILS_BERT_MODEL_DIR."
            )
        return Path(base_dir) / self._model_name

    def _ensure_loaded(self) -> tuple[Any, Any, Any]:
        if self._session is not None:
            return self._session, self._tokenizer, self._np

        model_dir = self._model_dir()
        model_path = model_dir / "model.onnx"
        tokenizer_path = model_dir / "tokenizer.json"

        if not model_path.is_file() or not tokenizer_path.is_file():
            raise GuardrailModelUnavailableError(
                f"ONNX model artifact not found at {model_dir} — expected "
                f"model.onnx and tokenizer.json. This is never downloaded "
                f"at runtime; supply it during image build or via a "
                f"customer-side artifact mount."
            )

        try:
            import numpy as np
            import onnxruntime as ort
            from tokenizers import Tokenizer

            session = ort.InferenceSession(str(model_path), providers=["CPUExecutionProvider"])
            tokenizer = Tokenizer.from_file(str(tokenizer_path))
        except Exception as exc:
            raise GuardrailModelUnavailableError(
                f"Failed to load ONNX model/tokenizer from {model_dir}: {exc}"
            ) from exc

        log.info("guardrails.bert.onnx_model_loaded", model_dir=str(model_dir))
        self._session = session
        self._tokenizer = tokenizer
        self._np = np
        self._toxic_label_indices = self._resolve_toxic_label_indices(model_dir, session)
        return session, tokenizer, np

    def _resolve_toxic_label_indices(self, model_dir: Path, session: Any) -> list[int]:
        """Reads id2label from config.json (standard HF export layout) to
        find which output logits correspond to a "toxic"-named label — same
        substring-match discipline the previous transformers-pipeline-based
        implementation used. Falls back to every output index (max-of-all)
        when no config.json/id2label is present."""
        num_outputs = self._output_width(session)
        config_path = model_dir / "config.json"
        if not config_path.is_file():
            return list(range(num_outputs))

        try:
            config = json.loads(config_path.read_text())
        except (OSError, ValueError):
            return list(range(num_outputs))

        id2label = config.get("id2label")
        if not isinstance(id2label, dict):
            return list(range(num_outputs))

        toxic_indices = [int(idx) for idx, label in id2label.items() if _is_toxic_label(str(label))]
        return toxic_indices or list(range(num_outputs))

    @staticmethod
    def _output_width(session: Any) -> int:
        try:
            shape = session.get_outputs()[0].shape
            width = shape[-1]
            return int(width) if isinstance(width, int) else 1
        except Exception:
            return 1

    def score(self, text: str) -> float:
        if not isinstance(text, str):
            raise TypeError(f"ONNXBertClassifier.score() expects str, got {type(text).__name__}")

        session, tokenizer, np = self._ensure_loaded()

        try:
            encoding = tokenizer.encode(text)
            input_ids = np.array([encoding.ids], dtype=np.int64)
            attention_mask = np.array([encoding.attention_mask], dtype=np.int64)

            input_names = {i.name for i in session.get_inputs()}
            feed: dict[str, Any] = {}
            if "input_ids" in input_names:
                feed["input_ids"] = input_ids
            if "attention_mask" in input_names:
                feed["attention_mask"] = attention_mask
            if "token_type_ids" in input_names:
                feed["token_type_ids"] = np.zeros_like(input_ids)

            (logits,) = session.run(None, feed)
            # unitary/toxic-bert is a multi-label classifier (sigmoid per
            # label, not a single softmax) — matches its real HF config.
            probs = 1.0 / (1.0 + np.exp(-logits[0]))

            candidates = self._toxic_label_indices or list(range(len(probs)))
            return float(max(probs[i] for i in candidates))
        except Exception as exc:
            raise GuardrailInferenceError(
                f"ONNX inference failed for model {self._model_name!r}: {exc}"
            ) from exc
