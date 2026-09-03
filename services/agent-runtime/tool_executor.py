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
"""

from __future__ import annotations

import hashlib
import json
import os
from typing import Any

import boto3

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
    def __init__(self, agent_id: str, tools: list[dict[str, Any]], lambda_client: Any | None = None) -> None:
        self._agent_id = agent_id
        self._tools = {t["tool_id"]: t for t in tools}
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

            function_name = tool_lambda_name(self._agent_id, tool_id)
            response = await asyncio.to_thread(
                self._lambda.invoke,
                FunctionName=function_name,
                InvocationType="RequestResponse",
                Payload=json.dumps(args).encode("utf-8"),
            )
            payload = json.loads(response["Payload"].read())
            if response.get("FunctionError"):
                results.append({"tool_id": tool_id, "error": payload})
            else:
                results.append({"tool_id": tool_id, "result": payload})

        return results
