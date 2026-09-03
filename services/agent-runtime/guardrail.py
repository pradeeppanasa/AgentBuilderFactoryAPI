"""Guardrail enforcement via Bedrock ApplyGuardrail.

AgentConfiguration.guardrail_policy_id references a row in the Factory
Runtime's guardrail policy catalog (panasa-guardrail-policies, keyed by
{tenant_id, policy_id}) — that record's bedrock_guardrail_id/
bedrock_guardrail_version are the real, AWS-provisioned guardrail this
applies. The guardrail itself is provisioned by the Factory Runtime's own
API directly against Bedrock (app/modules/guardrails/provisioner.py) when
the policy is saved, not by this agent's own Terraform (guardrails.tf.j2's
aws_bedrock_guardrail exists for the IaC validation suite's structural
checks — same "read the same catalog row the Console reads, never
Terraform state" pattern as rag_client.py).

Fail-closed, not fail-open: R39 applies to guardrails as a security
control (unlike Redis rate limiting, which R29 explicitly allows to fail
open) — if the guardrail can't be resolved or the Bedrock call itself
errors, this blocks rather than silently letting the message through.
"""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass
from typing import Any

import boto3

_BLOCKED_MESSAGE = "This request cannot be processed."


@dataclass(frozen=True)
class GuardrailResult:
    blocked: bool
    sanitised_text: str | None = None
    reason: str | None = None


class GuardrailChecker:
    def __init__(
        self,
        tenant_id: str,
        policy_id: str | None,
        dynamodb: Any | None = None,
        bedrock_runtime: Any | None = None,
    ) -> None:
        self._tenant_id = tenant_id
        self._policy_id = policy_id
        region = os.environ.get("AWS_REGION", "eu-west-2")
        self._dynamodb = dynamodb or boto3.resource("dynamodb", region_name=region)
        self._client = bedrock_runtime or boto3.client("bedrock-runtime", region_name=region)
        self._policy_table = os.environ.get(
            "DYNAMODB_GUARDRAIL_POLICIES_TABLE", "panasa-guardrail-policies"
        )
        self._resolved = False
        self._guardrail_id: str | None = None
        self._guardrail_version: str | None = None

    @property
    def enabled(self) -> bool:
        return bool(self._policy_id)

    async def _resolve(self) -> tuple[str, str] | None:
        if self._resolved:
            if self._guardrail_id and self._guardrail_version:
                return self._guardrail_id, self._guardrail_version
            return None
        self._resolved = True
        if not self._policy_id:
            return None
        table = self._dynamodb.Table(self._policy_table)
        response = await asyncio.to_thread(
            table.get_item, Key={"tenant_id": self._tenant_id, "policy_id": self._policy_id}
        )
        item = response.get("Item")
        if not item or not item.get("bedrock_guardrail_id"):
            return None
        self._guardrail_id = item["bedrock_guardrail_id"]
        self._guardrail_version = item.get("bedrock_guardrail_version") or "DRAFT"
        return self._guardrail_id, self._guardrail_version

    async def check(self, text: str, source: str = "INPUT") -> GuardrailResult:
        if not self.enabled:
            return GuardrailResult(blocked=False)

        resolved = await self._resolve()
        if resolved is None:
            # R39 — fail closed: a policy was configured but couldn't be
            # resolved to a real guardrail (not provisioned yet, catalog
            # row missing). Never silently skip enforcement.
            return GuardrailResult(blocked=True, reason="guardrail_not_resolved")

        guardrail_id, guardrail_version = resolved
        try:
            response = await asyncio.to_thread(
                self._client.apply_guardrail,
                guardrailIdentifier=guardrail_id,
                guardrailVersion=guardrail_version,
                source=source,
                content=[{"text": {"text": text}}],
            )
        except Exception:
            return GuardrailResult(blocked=True, reason="guardrail_call_failed")

        if response.get("action") == "GUARDRAIL_INTERVENED":
            outputs = response.get("outputs") or []
            sanitised = outputs[0].get("text") if outputs else None
            if sanitised is not None:
                return GuardrailResult(blocked=False, sanitised_text=sanitised)
            return GuardrailResult(blocked=True, reason="guardrail_intervened")

        return GuardrailResult(blocked=False)


def blocked_response_text() -> str:
    return _BLOCKED_MESSAGE
