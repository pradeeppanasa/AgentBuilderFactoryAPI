"""Builds a tiny, real, synthetic ONNX BERT-family classifier artifact for
tests/test_bert_classifier.py — never a real HF model download (fetching
real weights at test time would itself violate the "never download models
at runtime" rule ONNXBertClassifier enforces in production). This is a
genuine ONNX graph authored with the `onnx` package and run through a real
onnxruntime.InferenceSession — only the weights are synthetic, the
inference path is the real one.

Vocabulary/scoring design (single scalar embedded per vocab id, gathered
per-token, reduced via max over the sequence) — parameterized by
`positive_word`/`negative_word` so the same graph shape backs the
toxicity model (the original design) and, in one dedicated test, a second
check (e.g. nsfw) to prove ONNXBertClassifier's target_keyword
parameterization genuinely works against a real onnxruntime session:
    negative_word -> -6.0  (sigmoid ~0.0025 — clearly safe)
    positive_word  -> +6.0  (sigmoid ~0.9975 — clearly positive)
    "unsure"       ->  0.0  (sigmoid  0.5    — deliberately mid-band)
    everything else (incl. [CLS]/[SEP]/[UNK]/[PAD]) -> -6.0 (baseline safe)

Output layout matches a real HF export: logits[0] = negative class,
logits[1] = positive class, with a config.json id2label mapping so
ONNXBertClassifier's own keyword substring match exercises the same code
path a real export would.
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

_BASE_SPECIALS = {"[PAD]": 0, "[UNK]": 1, "[CLS]": 2, "[SEP]": 3}
_SAFE, _POSITIVE, _UNSURE = -6.0, 6.0, 0.0

MODEL_NAME = "unitary/toxic-bert"


def _build_vocab_and_embed(
    positive_word: str, negative_word: str
) -> tuple[dict[str, int], list[float]]:
    vocab = {**_BASE_SPECIALS, negative_word: 4, positive_word: 5, "unsure": 6}
    embed = [_SAFE, _SAFE, _SAFE, _SAFE, _SAFE, _POSITIVE, _UNSURE]
    return vocab, embed


def _build_tokenizer(path: Path, vocab: dict[str, int]) -> None:
    tok = Tokenizer(WordLevel(vocab=vocab, unk_token="[UNK]"))
    tok.normalizer = Lowercase()
    tok.pre_tokenizer = Whitespace()
    # Real BERT tokenizers always wrap input in [CLS] ... [SEP], including
    # for empty input (2 tokens minimum) — without this, an empty string
    # produces a zero-length sequence and onnxruntime's ReduceMax node
    # genuinely crashes (discovered while prototyping this fixture).
    tok.post_processor = TemplateProcessing(
        single="[CLS] $A [SEP]",
        special_tokens=[("[CLS]", vocab["[CLS]"]), ("[SEP]", vocab["[SEP]"])],
    )
    tok.save(str(path))


def _build_model(path: Path, embed: list[float]) -> None:
    input_ids = helper.make_tensor_value_info("input_ids", TensorProto.INT64, ["batch", "seq"])
    attention_mask = helper.make_tensor_value_info(
        "attention_mask", TensorProto.INT64, ["batch", "seq"]
    )
    logits = helper.make_tensor_value_info("logits", TensorProto.FLOAT, ["batch", 2])

    embed_init = helper.make_tensor("embed_table", TensorProto.FLOAT, [len(embed)], embed)

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
    return build_synthetic_model_dir_for(
        base_dir, model_name, positive_word="toxic", negative_word="safe", positive_label="toxic"
    )


def build_synthetic_model_dir_for(
    base_dir: Path,
    model_name: str,
    *,
    positive_word: str,
    negative_word: str,
    positive_label: str,
) -> Path:
    """Generalises build_synthetic_model_dir for an arbitrary Layer 1
    check (nsfw/injection/gibberish/...) — same real ONNX graph shape and
    real tokenizer, just a different vocab word/id2label pair, proving
    ONNXBertClassifier's target_keyword parameterization genuinely works
    against a real onnxruntime session for checks other than toxicity."""
    vocab, embed = _build_vocab_and_embed(positive_word, negative_word)
    model_dir = base_dir / model_name
    model_dir.mkdir(parents=True, exist_ok=True)
    _build_tokenizer(model_dir / "tokenizer.json", vocab)
    _build_model(model_dir / "model.onnx", embed)
    (model_dir / "config.json").write_text(
        json.dumps({"id2label": {"0": f"non_{positive_label}", "1": positive_label}})
    )
    return model_dir
