"""Auto-generates an AgentCapabilityContract from an AgentConfiguration (A1, R11).

User can edit `capabilities[]`, `description`, and `security_policy` afterwards
via PUT /api/v1/agents/{agent_id}/contract (added in a later phase). The
generator seeds conservative defaults so a contract always exists before an
agent can be deployed.
"""

from __future__ import annotations

from app.modules.registry.models import (
    AgentCapabilityContract,
    AgentConfiguration,
    AgentSecurityPolicy,
    AgentType,
)


class CapabilityContractGenerator:
    def generate(
        self,
        agent_id: str,
        agent_name: str,
        agent_type: AgentType,
        config: AgentConfiguration,
        version: int,
    ) -> AgentCapabilityContract:
        return AgentCapabilityContract(
            agent_id=agent_id,
            agent_name=agent_name,
            agent_type=agent_type,
            version=version,
            description=config.system_prompt[:200],  # Seeded from system prompt
            capabilities=[],  # User fills in — prompted in wizard Step 1
            accepted_input_schema=None,
            output_schema=config.output_format.json_schema,
            skills=[s.skill_id for s in config.skills if s.enabled],
            tools=[t.tool_id for t in config.tools],
            mcp_servers=[m.server_id for m in config.mcp_servers],
            knowledge_bases=(
                [config.knowledge_base.kb_name]
                if config.knowledge_base
                and config.knowledge_base.enabled
                and config.knowledge_base.kb_name
                else []
            ),
            allowed_actions=["read"],  # Conservative default
            restricted_actions=["external_write"],
            security_policy=AgentSecurityPolicy(
                guardrail_profile="standard",
                pii_policy="redact",
                data_classification="internal",
            ),
            latency_sla_ms=None,
            token_budget=config.max_tokens,
        )
