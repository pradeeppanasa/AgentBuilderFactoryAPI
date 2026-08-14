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
