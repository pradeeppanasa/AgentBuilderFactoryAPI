"""Builds a tiny, real, synthetic ONNX toxicity-classifier artifact for
tests/test_bert_classifier.py — never a real unitary/toxic-bert model
(fetching real weights at test time would itself violate the "never
download models at runtime" rule ONNXBertClassifier enforces in
production). This is a genuine ONNX graph authored with the `onnx` package
and run through a real onnxruntime.InferenceSession — only the weights are
synthetic, the inference path is the real one.

Vocabulary/scoring design (single scalar embedded per vocab id, gathered
per-token, reduced via max over the sequence):
    "safe"   -> -6.0  (sigmoid ~0.0025 — clearly safe)
    "toxic"  -> +6.0  (sigmoid ~0.9975 — clearly toxic)
    "unsure" ->  0.0  (sigmoid  0.5    — deliberately mid-band)
    everything else (incl. [CLS]/[SEP]/[UNK]/[PAD]) -> -6.0 (baseline safe)

Output layout matches a real HF export: logits[0] = non_toxic, logits[1] =
toxic, with a config.json id2label mapping so ONNXBertClassifier's own
"toxic"-label substring match exercises the same code path a real
unitary/toxic-bert export would.
"""

from __future__ import annotations

import json
from pathlib import Path

import onnx
from onnx import TensorProto, helper
from tokenizers import Tokenizer
from tokenizers.models import WordLevel
from tokenizers.normalizers import Lowercase
from tokenizers.pre_tokenizers import Whitespace
from tokenizers.processors import TemplateProcessing

VOCAB = {
    "[PAD]": 0,
    "[UNK]": 1,
    "[CLS]": 2,
    "[SEP]": 3,
    "safe": 4,
    "toxic": 5,
    "unsure": 6,
}
_SAFE, _TOXIC, _UNSURE = -6.0, 6.0, 0.0
EMBED = [_SAFE, _SAFE, _SAFE, _SAFE, _SAFE, _TOXIC, _UNSURE]

MODEL_NAME = "unitary/toxic-bert"


def _build_tokenizer(path: Path) -> None:
    tok = Tokenizer(WordLevel(vocab=VOCAB, unk_token="[UNK]"))
    tok.normalizer = Lowercase()
    tok.pre_tokenizer = Whitespace()
    # Real BERT tokenizers always wrap input in [CLS] ... [SEP], including
    # for empty input (2 tokens minimum) — without this, an empty string
    # produces a zero-length sequence and onnxruntime's ReduceMax node
    # genuinely crashes (discovered while prototyping this fixture).
    tok.post_processor = TemplateProcessing(
        single="[CLS] $A [SEP]",
        special_tokens=[("[CLS]", VOCAB["[CLS]"]), ("[SEP]", VOCAB["[SEP]"])],
    )
    tok.save(str(path))


def _build_model(path: Path) -> None:
    input_ids = helper.make_tensor_value_info("input_ids", TensorProto.INT64, ["batch", "seq"])
    attention_mask = helper.make_tensor_value_info(
        "attention_mask", TensorProto.INT64, ["batch", "seq"]
    )
    logits = helper.make_tensor_value_info("logits", TensorProto.FLOAT, ["batch", 2])

    embed_init = helper.make_tensor("embed_table", TensorProto.FLOAT, [len(EMBED)], EMBED)

    nodes = [
        helper.make_node("Gather", ["embed_table", "input_ids"], ["token_scores"]),
        helper.make_node("Cast", ["attention_mask"], ["mask_float"], to=TensorProto.FLOAT),
        helper.make_node(
            "Constant",
            [],
            ["one"],
            value=helper.make_tensor("one_val", TensorProto.FLOAT, [], [1.0]),
        ),
        helper.make_node(
            "Constant",
            [],
            ["neg_big"],
            value=helper.make_tensor("neg_big_val", TensorProto.FLOAT, [], [-100.0]),
        ),
        helper.make_node("Sub", ["one", "mask_float"], ["inv_mask"]),
        helper.make_node("Mul", ["inv_mask", "neg_big"], ["mask_penalty"]),
        helper.make_node("Mul", ["token_scores", "mask_float"], ["masked_scores"]),
        helper.make_node("Add", ["masked_scores", "mask_penalty"], ["final_scores"]),
        helper.make_node("ReduceMax", ["final_scores"], ["toxic_logit"], axes=[1], keepdims=1),
        helper.make_node("Neg", ["toxic_logit"], ["non_toxic_logit"]),
        helper.make_node("Concat", ["non_toxic_logit", "toxic_logit"], ["logits"], axis=1),
    ]

    graph = helper.make_graph(
        nodes,
        "toxicity-classifier",
        [input_ids, attention_mask],
        [logits],
        initializer=[embed_init],
    )
    model = helper.make_model(graph, opset_imports=[helper.make_opsetid("", 17)])
    model.ir_version = 8
    onnx.checker.check_model(model)
    onnx.save(model, str(path))


def build_synthetic_model_dir(base_dir: Path, model_name: str = MODEL_NAME) -> Path:
    """Creates `{base_dir}/{model_name}/{model.onnx, tokenizer.json,
    config.json}` and returns that model directory. `base_dir` is what a
    test sets `settings.guardrails_bert_model_dir` to."""
    model_dir = base_dir / model_name
    model_dir.mkdir(parents=True, exist_ok=True)
    _build_tokenizer(model_dir / "tokenizer.json")
    _build_model(model_dir / "model.onnx")
    (model_dir / "config.json").write_text(
        json.dumps({"id2label": {"0": "non_toxic", "1": "toxic"}})
    )
    return model_dir
