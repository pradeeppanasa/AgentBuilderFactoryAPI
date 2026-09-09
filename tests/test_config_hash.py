"""Sprint 4 Phase 5 — config_hash (S-13b, CLAUDE.md Section 62.4/R68)."""

from __future__ import annotations

from app.modules.registry.models import AgentConfiguration
from app.shared.config_hash import compute_config_hash


def _config(**overrides: object) -> AgentConfiguration:
    data: dict[str, object] = {
        "model_id": "anthropic.claude-3-5-sonnet-20241022-v2:0",
        "model_provider": "bedrock",
        "system_prompt": "You are a KYC verification agent.",
    }
    data.update(overrides)
    return AgentConfiguration(**data)


def test_hash_is_deterministic_for_the_same_config() -> None:
    config = _config()

    assert compute_config_hash(config) == compute_config_hash(config)


def test_hash_is_deterministic_across_separately_constructed_equal_configs() -> None:
    """Two independently-built model instances with identical field values
    must hash identically — this is what makes cross-service recomputation
    (config_loader.py) possible at all."""
    first = _config(temperature=0.2, max_tokens=1024)
    second = _config(temperature=0.2, max_tokens=1024)

    assert compute_config_hash(first) == compute_config_hash(second)


def test_hash_changes_when_a_field_changes() -> None:
    baseline = compute_config_hash(_config())
    changed = compute_config_hash(_config(temperature=0.9))

    assert baseline != changed


def test_hash_is_a_64_char_hex_sha256_digest() -> None:
    digest = compute_config_hash(_config())

    assert len(digest) == 64
    assert all(c in "0123456789abcdef" for c in digest)


def test_hash_survives_a_dynamodb_style_json_round_trip() -> None:
    """versioner.py stores `configuration` as a JSON string
    (json.dumps(model_dump(mode="json"))) and reads it back via
    json.loads() into a plain dict — recomputing the SAME canonical-JSON
    hash from that round-tripped dict (mirroring config_loader.py's own
    approach) must reproduce the exact same digest."""
    import hashlib
    import json

    config = _config(
        tools=[
            {
                "tool_id": "jira",
                "tool_name": "Jira",
                "executor_type": "http",
                "input_schema": {},
            }
        ]
    )
    original_hash = compute_config_hash(config)

    stored = json.dumps(config.model_dump(mode="json"))
    round_tripped = json.loads(stored)
    recomputed = hashlib.sha256(
        json.dumps(round_tripped, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()

    assert recomputed == original_hash
