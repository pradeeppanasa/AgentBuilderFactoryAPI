"""Circular dependency prevention for orchestrator sub-agent graphs (A5, R12).

Runs at three points per spec: on save, on deploy, on import. Phase 2 wires
the "on save" check — GET/PUT of an agent's orchestration.sub_agents runs
this against the full tenant agent graph before the write is committed.
"""

from __future__ import annotations

from pydantic import BaseModel


class ValidationResult(BaseModel):
    valid: bool
    reason: str | None = None


class CircularDependencyValidator:
    def validate(
        self,
        agent_id: str,
        proposed_sub_agents: list[str],
        all_agents: dict[str, list[str]],
    ) -> ValidationResult:
        """DFS cycle detection across the full agent graph.

        `all_agents` maps agent_id -> its current sub_agent ids (excluding
        `agent_id`, whose proposed edges are injected here for validation).
        """
        graph = dict(all_agents)
        graph[agent_id] = proposed_sub_agents

        visited: set[str] = set()
        rec_stack: set[str] = set()

        def has_cycle(node: str) -> bool:
            visited.add(node)
            rec_stack.add(node)
            for neighbour in graph.get(node, []):
                if neighbour not in visited:
                    if has_cycle(neighbour):
                        return True
                elif neighbour in rec_stack:
                    return True
            rec_stack.discard(node)
            return False

        if has_cycle(agent_id):
            return ValidationResult(
                valid=False,
                reason=(
                    f"Circular dependency detected: {agent_id} is reachable "
                    "from one of its sub-agents."
                ),
            )
        return ValidationResult(valid=True)
