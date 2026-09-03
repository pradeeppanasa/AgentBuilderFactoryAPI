from __future__ import annotations

from fakes import FakeDynamoDBResource, FakeTable

from memory import MemoryManager


async def test_none_memory_type_returns_empty_and_saves_nothing() -> None:
    manager = MemoryManager(agent_id="agent-1", memory_type="none")

    assert await manager.load(session_id="s1", user_id="u1") == ""
    await manager.save(session_id="s1", user_id="u1", message="hi", response="hello")
    assert await manager.load(session_id="s1", user_id="u1") == ""


async def test_session_memory_round_trips_within_same_session() -> None:
    manager = MemoryManager(agent_id="agent-1", memory_type="session")

    await manager.save(session_id="s1", user_id=None, message="What's your name?", response="Panasa Agent.")
    context = await manager.load(session_id="s1", user_id=None)

    assert "What's your name?" in context
    assert "Panasa Agent." in context


async def test_session_memory_is_scoped_per_session_id() -> None:
    manager = MemoryManager(agent_id="agent-1", memory_type="session")

    await manager.save(session_id="s1", user_id=None, message="secret to s1", response="ok")
    context = await manager.load(session_id="s2", user_id=None)

    assert context == ""


async def test_session_memory_caps_at_max_turns() -> None:
    manager = MemoryManager(agent_id="agent-1", memory_type="session", max_session_turns=2)

    for i in range(5):
        await manager.save(session_id="s1", user_id=None, message=f"turn-{i}", response=f"reply-{i}")

    context = await manager.load(session_id="s1", user_id=None)
    assert "turn-0" not in context
    assert "turn-4" in context
    assert context.count("User:") == 2


async def test_persistent_memory_requires_user_id() -> None:
    table = FakeTable(key_names=("pk", "memory_id"))
    resource = FakeDynamoDBResource({"panasa-memory": table})
    manager = MemoryManager(agent_id="agent-1", memory_type="persistent", dynamodb=resource)

    await manager.save(session_id="s1", user_id=None, message="hi", response="hello")

    assert table.put_calls == []


async def test_persistent_memory_saves_and_loads(monkeypatch) -> None:
    table = FakeTable(key_names=("pk", "memory_id"))
    resource = FakeDynamoDBResource({"panasa-memory": table})
    manager = MemoryManager(agent_id="agent-1", memory_type="persistent", ttl_days=30, dynamodb=resource)

    await manager.save(session_id="s1", user_id="user-42", message="My name is Alex.", response="Nice to meet you, Alex.")

    assert len(table.put_calls) == 1
    saved = table.put_calls[0]
    assert saved["pk"] == "agent-1#user-42"
    assert "Alex" in saved["content"]
    assert saved["expires_at"] > 0

    context = await manager.load(session_id="s1", user_id="user-42")
    assert "Alex" in context
