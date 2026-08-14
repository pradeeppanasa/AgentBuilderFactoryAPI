"""AgentConfiguration's Advanced Config additions (CLAUDE_Advanced_Config.md
Section 37.11): kb_id, guardrail_policy_id, model_advanced, memory_config,
tool_instances, output_schema. Additive alongside the pre-existing
knowledge_base/guardrails/tools/memory/output_format fields — see
app/modules/registry/models.py's comment above ModelAdvancedConfig for why
both sets coexist.
"""

from __future__ import annotations

from app.modules.registry.models import (
    AgentConfiguration,
    MemoryAdvancedConfig,
    ModelAdvancedConfig,
    OutputSchemaConfig,
    ToolInstanceConfig,
)


def _base_config(**overrides: object) -> AgentConfiguration:
    data: dict[str, object] = {
        "model_id": "anthropic.claude-3-5-sonnet-20241022-v2:0",
        "model_provider": "bedrock",
        "system_prompt": "You are a helpful agent.",
    }
    data.update(overrides)
    return AgentConfiguration(**data)


def test_new_fields_default_to_none_or_empty() -> None:
    config = _base_config()

    assert config.kb_id is None
    assert config.guardrail_policy_id is None
    assert config.model_advanced is None
    assert config.memory_config is None
    assert config.tool_instances == []
    assert config.output_schema is None


def test_new_fields_can_be_set_together() -> None:
    config = _base_config(
        kb_id="kb-abc123",
        guardrail_policy_id="pol-abc123",
        model_advanced=ModelAdvancedConfig(temperature=0.2, fallback_model_string="bedrock/x"),
        memory_config=MemoryAdvancedConfig(long_term_enabled=True, long_term_max_entries=500),
        tool_instances=[ToolInstanceConfig(connector_id="jira")],
        output_schema=OutputSchemaConfig(format="json", schema_definition={"type": "object"}),
    )

    assert config.kb_id == "kb-abc123"
    assert config.guardrail_policy_id == "pol-abc123"
    assert config.model_advanced is not None
    assert config.model_advanced.temperature == 0.2
    assert config.memory_config is not None
    assert config.memory_config.long_term_max_entries == 500
    assert len(config.tool_instances) == 1
    assert config.tool_instances[0].connector_id == "jira"
    assert config.output_schema is not None
    assert config.output_schema.format == "json"


def test_new_fields_coexist_with_pre_existing_older_fields() -> None:
    """The older `knowledge_base`/`tools`/`memory` fields are NOT removed —
    both shapes are populatable on the same instance (Section 37's spec text
    says "replaces X", but the explicit user instruction was to add these as
    new optional fields; removing the older ones would break the IaC
    generator/validator and their existing test coverage for no requested
    benefit)."""
    from app.modules.registry.models import KBConfig, MemoryConfig, ToolConfig

    config = _base_config(
        knowledge_base=KBConfig(enabled=True, kb_name="legacy-kb"),
        tools=[ToolConfig(tool_id="t1", tool_name="Tool", executor_type="http", input_schema={})],
        memory=MemoryConfig(memory_type="session"),
        kb_id="kb-new-shape",
        tool_instances=[ToolInstanceConfig(connector_id="c1")],
        memory_config=MemoryAdvancedConfig(session_enabled=True),
    )

    assert config.knowledge_base is not None and config.knowledge_base.kb_name == "legacy-kb"
    assert config.kb_id == "kb-new-shape"
    assert len(config.tools) == 1
    assert len(config.tool_instances) == 1
    assert config.memory.memory_type == "session"
    assert config.memory_config is not None and config.memory_config.session_enabled is True


def test_model_advanced_config_defaults() -> None:
    advanced = ModelAdvancedConfig()

    assert advanced.temperature == 0.7
    assert advanced.fallback_model_string is None
    assert advanced.cost_budget_usd is None


def test_tool_instance_config_defaults() -> None:
    instance = ToolInstanceConfig(connector_id="jira")

    assert instance.timeout_ms == 10000
    assert instance.error_handling == "fail_request"
    assert instance.fallback_connector_id is None


def test_output_schema_config_defaults() -> None:
    output_schema = OutputSchemaConfig()

    assert output_schema.format == "none"
    assert output_schema.schema_definition is None
    assert output_schema.fallback_on_max_retries == "return_error"
