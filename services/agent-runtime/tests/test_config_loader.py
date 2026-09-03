from __future__ import annotations

from decimal import Decimal

import pytest
from fakes import FakeDynamoDBResource, FakeTable

from config_loader import AgentNotFoundError, AgentVersionNotFoundError, load_agent_config


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


def test_falls_back_to_current_version_when_never_gone_live(monkeypatch: pytest.MonkeyPatch) -> None:
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
