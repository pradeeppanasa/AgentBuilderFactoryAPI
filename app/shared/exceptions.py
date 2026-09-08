"""Shared application exceptions. Mapped to HTTP responses at the API layer."""

from __future__ import annotations


class AgentBuilderError(Exception):
    """Base class for all domain errors raised by the runtime."""


class AgentNotFoundError(AgentBuilderError):
    def __init__(self, agent_id: str) -> None:
        self.agent_id = agent_id
        super().__init__(f"Agent {agent_id!r} not found")


class CircularDependencyError(AgentBuilderError):
    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


class VersionNotFoundError(AgentBuilderError):
    def __init__(self, agent_id: str, version: int) -> None:
        self.agent_id = agent_id
        self.version = version
        super().__init__(f"Version {version} of agent {agent_id!r} not found")


class InvalidRollbackError(AgentBuilderError):
    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


class NoApiKeyProvisionedError(AgentBuilderError):
    """Sprint 3 Phase 8 (S-02) — raised when rotate is called on an agent
    with no api_key_secret_arn yet (e.g. an enterprise-mode agent, whose key
    is provisioned by Terraform's random_password inside the customer VPC,
    R60 — never by this Runtime)."""

    def __init__(self, agent_id: str) -> None:
        self.agent_id = agent_id
        super().__init__(f"Agent {agent_id!r} has no API key provisioned to rotate")
