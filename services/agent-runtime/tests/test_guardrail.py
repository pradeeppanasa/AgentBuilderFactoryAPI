from __future__ import annotations

from fakes import FakeBedrockRuntimeClient, FakeDynamoDBResource, FakeTable

from guardrail import GuardrailChecker


def _resource_with_policy(policy_id: str, bedrock_guardrail_id: str | None, version: str = "1") -> FakeDynamoDBResource:
    table = FakeTable(key_names=("tenant_id", "policy_id"))
    if bedrock_guardrail_id is not None:
        table.items[("tenant-a", policy_id)] = {
            "tenant_id": "tenant-a",
            "policy_id": policy_id,
            "bedrock_guardrail_id": bedrock_guardrail_id,
            "bedrock_guardrail_version": version,
        }
    return FakeDynamoDBResource({"panasa-guardrail-policies": table})


async def test_no_policy_configured_never_calls_bedrock() -> None:
    bedrock = FakeBedrockRuntimeClient()
    checker = GuardrailChecker(tenant_id="tenant-a", policy_id=None, bedrock_runtime=bedrock)

    result = await checker.check("hello")

    assert result.blocked is False
    assert bedrock.calls == []


async def test_policy_configured_but_unresolvable_fails_closed() -> None:
    """R39 — guardrails are a security control, unlike Redis rate limiting
    (R29): unresolvable must block, never silently pass through."""
    resource = _resource_with_policy("policy-1", bedrock_guardrail_id=None)
    bedrock = FakeBedrockRuntimeClient()
    checker = GuardrailChecker(tenant_id="tenant-a", policy_id="policy-1", dynamodb=resource, bedrock_runtime=bedrock)

    result = await checker.check("hello")

    assert result.blocked is True
    assert result.reason == "guardrail_not_resolved"
    assert bedrock.calls == []


async def test_clean_content_passes() -> None:
    resource = _resource_with_policy("policy-1", "gr-123")
    bedrock = FakeBedrockRuntimeClient(action="NONE")
    checker = GuardrailChecker(tenant_id="tenant-a", policy_id="policy-1", dynamodb=resource, bedrock_runtime=bedrock)

    result = await checker.check("What's the weather?", source="INPUT")

    assert result.blocked is False
    assert bedrock.calls[0]["guardrailIdentifier"] == "gr-123"
    assert bedrock.calls[0]["guardrailVersion"] == "1"
    assert bedrock.calls[0]["source"] == "INPUT"


async def test_intervened_with_sanitised_text_redacts_rather_than_blocks() -> None:
    resource = _resource_with_policy("policy-1", "gr-123")
    bedrock = FakeBedrockRuntimeClient(
        action="GUARDRAIL_INTERVENED", outputs=[{"text": "My email is [REDACTED]."}]
    )
    checker = GuardrailChecker(tenant_id="tenant-a", policy_id="policy-1", dynamodb=resource, bedrock_runtime=bedrock)

    result = await checker.check("My email is a@b.com.")

    assert result.blocked is False
    assert result.sanitised_text == "My email is [REDACTED]."


async def test_intervened_with_no_output_text_blocks() -> None:
    resource = _resource_with_policy("policy-1", "gr-123")
    bedrock = FakeBedrockRuntimeClient(action="GUARDRAIL_INTERVENED", outputs=[])
    checker = GuardrailChecker(tenant_id="tenant-a", policy_id="policy-1", dynamodb=resource, bedrock_runtime=bedrock)

    result = await checker.check("something toxic")

    assert result.blocked is True
    assert result.reason == "guardrail_intervened"


async def test_bedrock_call_failure_fails_closed() -> None:
    resource = _resource_with_policy("policy-1", "gr-123")
    bedrock = FakeBedrockRuntimeClient(raise_error=True)
    checker = GuardrailChecker(tenant_id="tenant-a", policy_id="policy-1", dynamodb=resource, bedrock_runtime=bedrock)

    result = await checker.check("hello")

    assert result.blocked is True
    assert result.reason == "guardrail_call_failed"


async def test_resolution_is_cached_across_calls() -> None:
    resource = _resource_with_policy("policy-1", "gr-123")
    bedrock = FakeBedrockRuntimeClient(action="NONE")
    checker = GuardrailChecker(tenant_id="tenant-a", policy_id="policy-1", dynamodb=resource, bedrock_runtime=bedrock)

    await checker.check("first")
    await checker.check("second")

    # get_item on the policy table only happens once, not per request —
    # avoids a DynamoDB round trip on every single guardrail check.
    assert len(resource.Table("panasa-guardrail-policies").get_calls) == 1
    assert len(bedrock.calls) == 2
