"""KBConfig's hybrid-search retrieval settings (2026-08-23): hybrid_mode,
fusion_weight, reranker_model, top_k, filter_threshold — BM25/lexical +
vector/semantic search -> result fusion -> cross-encoder reranker ->
top-K selection -> context filtering.

Config-only, per F8/R30: this Builder Runtime stores and validates these
fields alongside the rest of KBConfig — it never runs retrieval itself
(app/api/v1/playground.py's _run_kb_retrieval stays a stub). The Generated
Agent Runtime is the separate service responsible for actually building
and running the pipeline these fields describe.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.modules.registry.models import KBConfig


def test_hybrid_search_fields_default_off() -> None:
    config = KBConfig()

    assert config.hybrid_mode is False
    assert config.fusion_weight == 0.5
    assert config.reranker_model is None
    assert config.filter_threshold is None
    assert config.top_k == 5


def test_hybrid_search_fields_can_be_set_together() -> None:
    config = KBConfig(
        hybrid_mode=True,
        fusion_weight=0.7,
        reranker_model="cross-encoder/ms-marco-MiniLM-L-6-v2",
        top_k=10,
        filter_threshold=0.3,
    )

    assert config.hybrid_mode is True
    assert config.fusion_weight == 0.7
    assert config.reranker_model == "cross-encoder/ms-marco-MiniLM-L-6-v2"
    assert config.top_k == 10
    assert config.filter_threshold == 0.3


def test_hybrid_search_fields_coexist_with_pre_existing_kb_fields() -> None:
    config = KBConfig(
        enabled=True,
        kb_id="kb-abc123",
        embedding_model="amazon.titan-embed-text-v2:0",
        reranking_enabled=True,
        hybrid_mode=True,
        fusion_weight=0.6,
    )

    assert config.enabled is True
    assert config.kb_id == "kb-abc123"
    assert config.reranking_enabled is True
    assert config.hybrid_mode is True


@pytest.mark.parametrize("fusion_weight", [-0.1, 1.1])
def test_fusion_weight_out_of_range_rejected(fusion_weight: float) -> None:
    with pytest.raises(ValidationError):
        KBConfig(fusion_weight=fusion_weight)


@pytest.mark.parametrize("filter_threshold", [-0.1, 1.1])
def test_filter_threshold_out_of_range_rejected(filter_threshold: float) -> None:
    with pytest.raises(ValidationError):
        KBConfig(filter_threshold=filter_threshold)


@pytest.mark.parametrize("top_k", [0, -1])
def test_top_k_must_be_positive(top_k: int) -> None:
    with pytest.raises(ValidationError):
        KBConfig(top_k=top_k)


def test_filter_threshold_none_means_no_filtering() -> None:
    """None is the explicit "don't filter" sentinel, not a value that must
    fall in the [0, 1] validated range."""
    config = KBConfig(filter_threshold=None)
    assert config.filter_threshold is None
