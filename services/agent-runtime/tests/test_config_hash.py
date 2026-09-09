from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

from config_hash import compute_config_hash

_MAIN_REPO_ROOT = Path(__file__).resolve().parents[3]


def test_hash_is_deterministic() -> None:
    config = {"model_id": "m", "model_provider": "bedrock", "system_prompt": "hi"}

    assert compute_config_hash(config) == compute_config_hash(config)


def test_hash_is_order_insensitive() -> None:
    forward = {"a": 1, "b": 2, "c": 3}
    reversed_ = {"c": 3, "b": 2, "a": 1}

    assert compute_config_hash(forward) == compute_config_hash(reversed_)


def test_hash_changes_when_a_value_changes() -> None:
    baseline = {"model_id": "m", "temperature": 0.3}
    changed = {"model_id": "m", "temperature": 0.9}

    assert compute_config_hash(baseline) != compute_config_hash(changed)


def test_hash_matches_app_shared_config_hash_for_the_same_configuration() -> None:
    """Cross-service pinning test — mirrors test_tool_executor.py's
    test_lambda_name_matches_terraform_naming_convention in spirit
    (F8: services/agent-runtime and the Factory Runtime share zero code,
    so this is the only thing that would catch the two independently
    duplicated hash algorithms silently drifting apart). Imports the
    REAL Factory Runtime module and the REAL AgentConfiguration model,
    builds an equivalent config on both sides, and confirms identical
    digests."""
    sys.path.insert(0, str(_MAIN_REPO_ROOT))
    try:
        from app.modules.registry.models import AgentConfiguration
        from app.shared.config_hash import compute_config_hash as factory_compute_config_hash
    finally:
        sys.path.remove(str(_MAIN_REPO_ROOT))

    config = AgentConfiguration(
        model_id="anthropic.claude-3-5-sonnet-20241022-v2:0",
        model_provider="bedrock",
        system_prompt="You are a FAQ agent.",
    )
    factory_hash = factory_compute_config_hash(config)

    # Mirrors the real round trip: versioner.py stores
    # json.dumps(model_dump(mode="json")); config_loader.py loads that
    # back with json.loads() before hashing — never the live Pydantic
    # object, matching this service's own zero-Pydantic-dependency rule.
    import json

    round_tripped: dict[str, Any] = json.loads(json.dumps(config.model_dump(mode="json")))

    assert compute_config_hash(round_tripped) == factory_hash
