"""Tool execution — R44: every tool call goes Lambda -> Secrets Manager ->
external API. No Composio. No direct HTTP calls from this runtime.

The Lambda this invokes is the one tools.tf.j2 creates for this exact tool
(function_name = tool_lambda_name(agent_id, tool_id)) — that truncation
logic is duplicated here rather than imported (F8: this is a separate
deployable service with zero shared code with the Factory Runtime). It
MUST stay byte-for-byte identical to app/modules/iac_generator/naming.py's
tool_lambda_name(); tests/test_tool_executor.py's
test_lambda_name_matches_terraform_naming_convention pins the exact
truncation behaviour so the two can't silently drift apart.

Sprint 3 Phase 2 (CLAUDE.md Section 61.2) — every tool call writes a
"tool.invoked" audit event, success or failure.

Sprint 3 Phase 4 (CLAUDE.md Section 64.3, S-08, R64) — every KNOWN tool
call is checked against this agent's tool_policies before the Lambda is
ever invoked. ToolPolicyEngine.enforce() raises ToolDeniedError (default
deny, or explicitly disabled) or ApprovalRequiredError (HIGH/DESTRUCTIVE
risk) — both propagate out of execute() rather than being swallowed into
a results-list entry like "unknown tool"/"invalid arguments" are, since
main.py's /chat handler needs to turn them into a distinct HTTP 403/202,
not a 200 with an error string buried in the body.
"""

from __future__ import annotations

import hashlib
import json
import os
from typing import Any

import boto3
from audit import write_audit_event
from tool_policy import ToolPolicyEngine

_ID_HASH_LEN = 6
_LAMBDA_NAME_MAX = 64


def tool_lambda_name(agent_id: str, tool_id: str) -> str:
    identifier = f"{agent_id}-tool-{tool_id}"
    full = f"panasa-{identifier}"
    if len(full) <= _LAMBDA_NAME_MAX:
        return full
    budget = _LAMBDA_NAME_MAX - len("panasa-") - 1 - _ID_HASH_LEN
    truncated = identifier[:budget]
    id_hash = hashlib.sha1(identifier.encode("utf-8")).hexdigest()[:_ID_HASH_LEN]
    return f"panasa-{truncated}-{id_hash}"


class ToolExecutionError(RuntimeError):
    def __init__(self, tool_id: str, reason: str) -> None:
        self.tool_id = tool_id
        self.reason = reason
        super().__init__(f"Tool {tool_id!r} execution failed: {reason}")


class ToolExecutor:
    def __init__(
        self,
        agent_id: str,
        tenant_id: str,
        tools: list[dict[str, Any]],
        tool_policies: list[dict[str, Any]] | None = None,
        lambda_client: Any | None = None,
    ) -> None:
        self._agent_id = agent_id
        self._tenant_id = tenant_id
        self._tools = {t["tool_id"]: t for t in tools}
        self._policy_engine = ToolPolicyEngine(tool_policies or [])
        self._lambda = lambda_client or boto3.client(
            "lambda", region_name=os.environ.get("AWS_REGION", "eu-west-2")
        )

    def get_definitions(self) -> list[dict[str, Any]]:
        """Tool definitions in OpenAI/LiteLLM function-calling shape."""
        return [
            {
                "type": "function",
                "function": {
                    "name": tool["tool_id"],
                    "description": tool.get("tool_name", tool["tool_id"]),
                    "parameters": tool.get("input_schema") or {"type": "object", "properties": {}},
                },
            }
            for tool in self._tools.values()
        ]

    async def execute(self, tool_calls: list[dict[str, Any]]) -> list[dict[str, Any]]:
        import asyncio

        results = []
        for call in tool_calls:
            tool_id = call["name"]
            if tool_id not in self._tools:
                results.append({"tool_id": tool_id, "error": "unknown tool"})
                continue
            try:
                args = json.loads(call["arguments"]) if call.get("arguments") else {}
            except json.JSONDecodeError as exc:
                results.append({"tool_id": tool_id, "error": f"invalid arguments: {exc}"})
                continue

            # Sprint 3 Phase 4 (R64) — raises ToolDeniedError/
            # ApprovalRequiredError, propagated out of execute() rather
            # than appended as a results-list entry (see module docstring).
            self._policy_engine.enforce(
                tool_id,
                tenant_id=self._tenant_id,
                agent_id=self._agent_id,
                principal_id="agent-runtime",
            )

            function_name = tool_lambda_name(self._agent_id, tool_id)
            response = await asyncio.to_thread(
                self._lambda.invoke,
                FunctionName=function_name,
                InvocationType="RequestResponse",
                Payload=json.dumps(args).encode("utf-8"),
            )
            payload = json.loads(response["Payload"].read())
            tool_failed = bool(response.get("FunctionError"))
            if tool_failed:
                results.append({"tool_id": tool_id, "error": payload})
            else:
                results.append({"tool_id": tool_id, "result": payload})

            write_audit_event(
                tenant_id=self._tenant_id,
                event_type="tool.invoked",
                agent_id=self._agent_id,
                principal_id="agent-runtime",
                action=f"invoke:{tool_id}",
                resource=tool_id,
                result="error" if tool_failed else "success",
            )
            if tool_failed:
                # Sprint 4 Phase 2 (S-05, CLAUDE.md Section 61.2) —
                # tool.invoked (above) records every attempt regardless of
                # outcome; tool.failed is the taxonomy's separate,
                # specifically-filterable event for the Lambda-returned-
                # error case, written alongside it rather than instead of
                # it.
                write_audit_event(
                    tenant_id=self._tenant_id,
                    event_type="tool.failed",
                    agent_id=self._agent_id,
                    principal_id="agent-runtime",
                    action=f"invoke:{tool_id}",
                    resource=tool_id,
                    result="error",
                )

        return results
