from __future__ import annotations

from fakes import FakeBedrockAgentRuntimeClient, FakeDynamoDBResource, FakeTable

from rag_client import RAGClient


def _catalog(kb_id: str, bedrock_kb_id: str | None) -> FakeDynamoDBResource:
    table = FakeTable(key_names=("tenant_id", "kb_id"))
    if bedrock_kb_id is not None:
        table.items[("tenant-a", kb_id)] = {
            "tenant_id": "tenant-a",
            "kb_id": kb_id,
            "bedrock_kb_id": bedrock_kb_id,
        }
    return FakeDynamoDBResource({"panasa-knowledge-bases": table})


async def test_disabled_when_no_kb_config() -> None:
    client = RAGClient(tenant_id="tenant-a", kb_config=None)
    assert client.enabled is False
    assert await client.retrieve("anything") == ""


async def test_disabled_when_enabled_false() -> None:
    client = RAGClient(tenant_id="tenant-a", kb_config={"enabled": False, "kb_id": "kb-1"})
    assert client.enabled is False


async def test_resolves_bedrock_kb_id_from_catalog_and_retrieves() -> None:
    dynamodb = _catalog("kb-1", "AWSKB123")
    bedrock_agent_runtime = FakeBedrockAgentRuntimeClient(
        results=[
            {"content": {"text": "Refunds are processed within 30 days."}},
            {"content": {"text": "Contact support@acme.com for help."}},
        ]
    )
    client = RAGClient(
        tenant_id="tenant-a",
        kb_config={"enabled": True, "kb_id": "kb-1", "top_k": 3},
        dynamodb=dynamodb,
        bedrock_agent_runtime=bedrock_agent_runtime,
    )

    context = await client.retrieve("What is the refund policy?")

    assert "Refunds are processed within 30 days." in context
    assert "Contact support@acme.com for help." in context
    call = bedrock_agent_runtime.calls[0]
    assert call["knowledgeBaseId"] == "AWSKB123"
    assert call["retrievalConfiguration"]["vectorSearchConfiguration"]["numberOfResults"] == 3


async def test_unresolvable_kb_id_returns_empty_without_calling_bedrock() -> None:
    dynamodb = _catalog("kb-1", bedrock_kb_id=None)
    bedrock_agent_runtime = FakeBedrockAgentRuntimeClient()
    client = RAGClient(
        tenant_id="tenant-a",
        kb_config={"enabled": True, "kb_id": "kb-1"},
        dynamodb=dynamodb,
        bedrock_agent_runtime=bedrock_agent_runtime,
    )

    context = await client.retrieve("anything")

    assert context == ""
    assert bedrock_agent_runtime.calls == []


async def test_resolution_is_cached_across_calls() -> None:
    dynamodb = _catalog("kb-1", "AWSKB123")
    bedrock_agent_runtime = FakeBedrockAgentRuntimeClient(results=[])
    client = RAGClient(
        tenant_id="tenant-a",
        kb_config={"enabled": True, "kb_id": "kb-1"},
        dynamodb=dynamodb,
        bedrock_agent_runtime=bedrock_agent_runtime,
    )

    await client.retrieve("first")
    await client.retrieve("second")

    assert len(dynamodb.Table("panasa-knowledge-bases").get_calls) == 1
    assert len(bedrock_agent_runtime.calls) == 2
