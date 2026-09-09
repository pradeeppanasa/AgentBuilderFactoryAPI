from __future__ import annotations

import json
from decimal import Decimal
from typing import Any

import config_loader as config_loader_module
import pytest
from config_hash import compute_config_hash
from config_loader import (
    AgentNotFoundError,
    AgentVersionNotFoundError,
    ConfigIntegrityError,
    load_agent_config,
)

from fakes import FakeDynamoDBResource, FakeTable


def _resource(agents_item: dict | None, version_item: dict | None) -> FakeDynamoDBResource:
    agents_table = FakeTable(key_names=("tenant_id", "agent_id"))
    if agents_item is not None:
        agents_table.items[("tenant-a", "faq-agent-1")] = agents_item

    versions_table = FakeTable(key_names=("agent_id", "version"))
    if version_item is not None:
        versions_table.items[("faq-agent-1", version_item["version"])] = version_item

    return FakeDynamoDBResource(
        {"panasa-agents": agents_table, "panasa-agent-versions": versions_table}
    )


def _set_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENT_ID", "faq-agent-1")
    monkeypatch.setenv("TENANT_ID", "tenant-a")


def test_loads_live_version_config(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_env(monkeypatch)
    resource = _resource(
        agents_item={
            "tenant_id": "tenant-a",
            "agent_id": "faq-agent-1",
            "name": "FAQ Agent",
            "current_version": 3,
            "live_version": 2,
        },
        version_item={
            "agent_id": "faq-agent-1",
            "version": 2,
            "configuration": {
                "model_id": "anthropic.claude-3-5-sonnet-20241022-v2:0",
                "model_provider": "bedrock",
                "system_prompt": "You are a FAQ agent.",
                "max_tokens": Decimal(2048),
                "temperature": Decimal("0.3"),
            },
        },
    )

    config = load_agent_config(dynamodb=resource)

    assert config["agent_id"] == "faq-agent-1"
    assert config["tenant_id"] == "tenant-a"
    assert config["name"] == "FAQ Agent"
    assert config["version"] == 2  # live_version, not current_version
    assert config["model_id"] == "anthropic.claude-3-5-sonnet-20241022-v2:0"
    assert config["max_tokens"] == 2048
    assert isinstance(config["max_tokens"], int)
    assert config["temperature"] == 0.3
    assert isinstance(config["temperature"], float)


def test_loads_config_when_configuration_is_stored_as_a_json_string(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Sprint 4 Phase 5 (S-13b) regression — app/modules/registry/
    versioner.py's real _to_item() stores `configuration` as a JSON
    STRING (its _JSON_FIELDS set), never a native DynamoDB Map. Every
    OTHER test in this file passes it as a plain dict, which is why this
    exact shape mismatch went uncaught until confirmed against a real
    moto-backed table (see CLAUDE.md's S-13b status note)."""
    _set_env(monkeypatch)
    resource = _resource(
        agents_item={
            "tenant_id": "tenant-a",
            "agent_id": "faq-agent-1",
            "name": "FAQ Agent",
            "current_version": 1,
            "live_version": 1,
        },
        version_item={
            "agent_id": "faq-agent-1",
            "version": 1,
            "configuration": json.dumps(
                {
                    "model_id": "anthropic.claude-3-5-sonnet-20241022-v2:0",
                    "model_provider": "bedrock",
                    "system_prompt": "You are a FAQ agent.",
                }
            ),
        },
    )

    config = load_agent_config(dynamodb=resource)

    assert config["model_id"] == "anthropic.claude-3-5-sonnet-20241022-v2:0"
    assert config["system_prompt"] == "You are a FAQ agent."


def test_falls_back_to_current_version_when_never_gone_live(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The very first deploy's own health check IS what marks live_version
    — it can't already be set when this container is the one being health
    checked. current_version (the version just applied) is what resolves
    that bootstrap chicken-and-egg."""
    _set_env(monkeypatch)
    resource = _resource(
        agents_item={
            "tenant_id": "tenant-a",
            "agent_id": "faq-agent-1",
            "name": "FAQ Agent",
            "current_version": 1,
            "live_version": None,
        },
        version_item={
            "agent_id": "faq-agent-1",
            "version": 1,
            "configuration": {
                "model_id": "anthropic.claude-3-5-haiku-20241022-v1:0",
                "model_provider": "bedrock",
                "system_prompt": "Hello.",
            },
        },
    )

    config = load_agent_config(dynamodb=resource)
    assert config["version"] == 1


def test_raises_when_agent_record_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_env(monkeypatch)
    resource = _resource(agents_item=None, version_item=None)

    with pytest.raises(AgentNotFoundError):
        load_agent_config(dynamodb=resource)


def test_raises_when_version_record_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_env(monkeypatch)
    resource = _resource(
        agents_item={
            "tenant_id": "tenant-a",
            "agent_id": "faq-agent-1",
            "name": "FAQ Agent",
            "current_version": 1,
            "live_version": None,
        },
        version_item=None,
    )

    with pytest.raises(AgentVersionNotFoundError):
        load_agent_config(dynamodb=resource)


# ── Sprint 4 Phase 5 (S-13b, R68) — AGENT_CONFIG_HASH verification ──────

_TEST_CONFIGURATION = {
    "model_id": "anthropic.claude-3-5-sonnet-20241022-v2:0",
    "model_provider": "bedrock",
    "system_prompt": "You are a FAQ agent.",
}


def _agents_item() -> dict[str, Any]:
    return {
        "tenant_id": "tenant-a",
        "agent_id": "faq-agent-1",
        "name": "FAQ Agent",
        "current_version": 1,
        "live_version": 1,
    }


def _version_item(configuration: dict[str, Any] = _TEST_CONFIGURATION) -> dict[str, Any]:
    return {"agent_id": "faq-agent-1", "version": 1, "configuration": dict(configuration)}


def test_load_succeeds_when_no_hash_env_var_is_set(monkeypatch: pytest.MonkeyPatch) -> None:
    """No AGENT_CONFIG_HASH at all — an agent deployed before this feature
    existed, or a local/dev run. Skipped, not failed."""
    _set_env(monkeypatch)
    resource = _resource(agents_item=_agents_item(), version_item=_version_item())

    config = load_agent_config(dynamodb=resource)

    assert config["model_id"] == "anthropic.claude-3-5-sonnet-20241022-v2:0"


def test_load_succeeds_when_hash_matches(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_env(monkeypatch)
    monkeypatch.setenv("AGENT_CONFIG_HASH", compute_config_hash(_TEST_CONFIGURATION))
    resource = _resource(agents_item=_agents_item(), version_item=_version_item())

    config = load_agent_config(dynamodb=resource)

    assert config["model_id"] == "anthropic.claude-3-5-sonnet-20241022-v2:0"


def test_load_raises_and_writes_audit_event_when_hash_mismatches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_env(monkeypatch)
    monkeypatch.setenv("AGENT_CONFIG_HASH", "0" * 64)  # deliberately wrong
    resource = _resource(agents_item=_agents_item(), version_item=_version_item())

    events: list[dict[str, Any]] = []
    monkeypatch.setattr(
        config_loader_module, "write_audit_event", lambda **kwargs: events.append(kwargs)
    )

    with pytest.raises(ConfigIntegrityError) as excinfo:
        load_agent_config(dynamodb=resource)

    assert excinfo.value.agent_id == "faq-agent-1"
    assert excinfo.value.expected == "0" * 64
    assert len(events) == 1
    assert events[0]["event_type"] == "agent.config_integrity_failed"
    assert events[0]["tenant_id"] == "tenant-a"
    assert events[0]["agent_id"] == "faq-agent-1"
    assert events[0]["result"] == "denied"
    assert events[0]["extra"]["expected_hash"] == "0" * 64


def test_load_hash_check_is_insensitive_to_dict_key_order(monkeypatch: pytest.MonkeyPatch) -> None:
    """The hash must be computed the same way regardless of DynamoDB/JSON
    round-trip key ordering — sort_keys=True canonicalisation, not
    insertion order, is what makes this safe."""
    _set_env(monkeypatch)
    reordered = dict(reversed(list(_TEST_CONFIGURATION.items())))
    assert list(reordered.keys()) != list(_TEST_CONFIGURATION.keys())  # sanity: genuinely reordered

    monkeypatch.setenv("AGENT_CONFIG_HASH", compute_config_hash(_TEST_CONFIGURATION))
    resource = _resource(agents_item=_agents_item(), version_item=_version_item(reordered))

    config = load_agent_config(dynamodb=resource)

    assert config["model_id"] == "anthropic.claude-3-5-sonnet-20241022-v2:0"
