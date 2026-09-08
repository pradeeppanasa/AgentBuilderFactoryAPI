"""Tool Policy Engine (Sprint 3 Phase 4 — CLAUDE.md Section 64.3, S-08,
R64).

Default-deny: a tool_id absent from this agent's tool_policies list
cannot be invoked, even if it is configured in `tools` (the two lists
serve different purposes — `tools` says what Lambda/endpoint exists for a
tool_id; `tool_policies` says whether THIS agent is allowed to call it and
at what risk level).

HIGH/DESTRUCTIVE tools require human approval. Phase 4 implements this as
the CLAUDE.md instruction's own stub: the call is never silently blocked
forever nor silently allowed — ApprovalRequiredError is raised immediately
and main.py's /chat handler turns that into an HTTP 202
awaiting_approval response. The full async approval flow (a human
actually clicking approve/reject, and the tool then running) is S-11
(P1) — not built here.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any

from audit import write_audit_event


class RiskLevel(str, Enum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    DESTRUCTIVE = "DESTRUCTIVE"


_REQUIRES_APPROVAL = {RiskLevel.HIGH, RiskLevel.DESTRUCTIVE}


class ToolDeniedError(Exception):
    def __init__(self, tool_id: str, reason: str) -> None:
        self.tool_id = tool_id
        self.reason = reason
        super().__init__(f"Tool {tool_id!r} denied: {reason}")


class ApprovalRequiredError(Exception):
    def __init__(self, tool_id: str) -> None:
        self.tool_id = tool_id
        super().__init__(f"Tool {tool_id!r} requires human approval")


@dataclass
class PolicyDecision:
    allowed: bool
    requires_approval: bool
    reason: str


class ToolPolicyEngine:
    def __init__(self, tool_policies: list[dict[str, Any]]) -> None:
        self._policies = {p["tool"]: p for p in tool_policies}

    def check(self, tool_id: str) -> PolicyDecision:
        """Pure decision, no side effects — enforce() below is what
        writes audit events and raises. Kept separate so a caller (or a
        test) can inspect the decision without also triggering an audit
        write."""
        policy = self._policies.get(tool_id)

        if policy is None:
            return PolicyDecision(
                allowed=False,
                requires_approval=False,
                reason=f"Tool {tool_id!r} not in agent tool policy — default deny",
            )

        if not policy.get("allowed", False):
            return PolicyDecision(
                allowed=False,
                requires_approval=False,
                reason=f"Tool {tool_id!r} is disabled in policy",
            )

        risk = RiskLevel(policy.get("risk", "LOW"))
        if risk in _REQUIRES_APPROVAL:
            return PolicyDecision(
                allowed=True,
                requires_approval=True,
                reason=f"Risk level {risk.value} requires human approval",
            )

        return PolicyDecision(allowed=True, requires_approval=False, reason="ok")

    def enforce(self, tool_id: str, tenant_id: str, agent_id: str, principal_id: str) -> None:
        """Raises ToolDeniedError or ApprovalRequiredError if the tool may
        not proceed right now; returns None (silently) if it may. Denial
        and approval-required both write their own audit event here —
        `tool.invoked` on actual execution remains tool_executor.py's own
        responsibility (Phase 2, unchanged)."""
        decision = self.check(tool_id)

        if not decision.allowed:
            write_audit_event(
                tenant_id=tenant_id,
                event_type="tool.denied",
                agent_id=agent_id,
                principal_id=principal_id,
                action=f"invoke:{tool_id}",
                resource=tool_id,
                result="denied",
                extra={"reason": decision.reason},
            )
            raise ToolDeniedError(tool_id, decision.reason)

        if decision.requires_approval:
            write_audit_event(
                tenant_id=tenant_id,
                event_type="human.approval.requested",
                agent_id=agent_id,
                principal_id=principal_id,
                action=f"invoke:{tool_id}",
                resource=tool_id,
                result="pending",
                extra={"reason": decision.reason},
            )
            raise ApprovalRequiredError(tool_id)
