"""Sprint 4 Phase 2 (S-05) — hash-chain tamper-evidence on
SecurityAuditLogStore.write_event, and TTL/PITR enablement at table
creation time."""

from __future__ import annotations

import boto3

from app.config import settings
from app.modules.audit.security_log import SecurityAuditLogStore

TENANT_A = "tenant-a"
TENANT_B = "tenant-b"


async def _make_store() -> SecurityAuditLogStore:
    dynamodb = boto3.resource("dynamodb", region_name="eu-west-2")
    store = SecurityAuditLogStore(dynamodb, settings)
    await store.ensure_table()
    return store


async def _write(store: SecurityAuditLogStore, tenant_id: str, event_type: str) -> None:
    await store.write_event(
        tenant_id=tenant_id,
        event_type=event_type,
        agent_id="agent-1",
        principal_id="dev@example.com",
        action=event_type,
        resource="agent-1",
        result="success",
    )


def _events_in_order(tenant_id: str) -> list[dict]:
    """Reconstructs write order by following the prev_hash/event_hash
    chain, NOT by sorting on `timestamp` — the table's real sort key is
    event_id (a random UUID), not timestamp, so two events written within
    the same clock tick can tie on timestamp and then sort arbitrarily by
    UUID instead of true write order (reproduced: this flaked exactly that
    way on a fast run). Following the chain links is both robust to that
    and a more direct test of the actual feature under test."""
    dynamodb = boto3.resource("dynamodb", region_name="eu-west-2")
    table = dynamodb.Table(settings.dynamodb_audit_log_table)
    response = table.query(
        KeyConditionExpression=boto3.dynamodb.conditions.Key("tenant_id").eq(tenant_id)
    )
    items = {
        i["event_hash"]: i for i in response.get("Items", []) if i["event_id"] != "_CHAIN_TAIL_"
    }
    by_prev_hash = {i["prev_hash"]: i for i in items.values()}

    ordered: list[dict] = []
    current = by_prev_hash.get(None)
    while current is not None:
        ordered.append(current)
        current = by_prev_hash.get(current["event_hash"])
    assert len(ordered) == len(items), "chain is broken — not every event was reachable from head"
    return ordered


async def test_first_event_for_a_tenant_has_no_prev_hash() -> None:
    store = await _make_store()
    await _write(store, TENANT_A, "agent.created")

    events = _events_in_order(TENANT_A)
    assert len(events) == 1
    assert events[0]["prev_hash"] is None
    assert events[0]["event_hash"]


async def test_second_event_references_first_events_hash() -> None:
    store = await _make_store()
    await _write(store, TENANT_A, "agent.created")
    await _write(store, TENANT_A, "agent.updated")

    events = _events_in_order(TENANT_A)
    assert len(events) == 2
    assert events[1]["prev_hash"] == events[0]["event_hash"]


async def test_chains_are_independent_per_tenant() -> None:
    store = await _make_store()
    await _write(store, TENANT_A, "agent.created")
    await _write(store, TENANT_B, "agent.created")
    await _write(store, TENANT_A, "agent.updated")

    tenant_a_events = _events_in_order(TENANT_A)
    tenant_b_events = _events_in_order(TENANT_B)

    assert len(tenant_a_events) == 2
    assert len(tenant_b_events) == 1
    # tenant-b's single event must not reference anything from tenant-a's chain.
    assert tenant_b_events[0]["prev_hash"] is None


async def test_event_hash_is_a_deterministic_sha256_over_the_event_content() -> None:
    """A real tamper-check tool re-reads the item from DynamoDB (Decimal
    types for every number) and must decimal_to_native() it before
    re-hashing — write_event() itself hashes native Python types (a plain
    int expires_at) before the item ever touches DynamoDB, so hashing the
    raw Decimal-bearing read-back would always mismatch even with zero
    tampering (Decimal(123) and int(123) stringify differently under
    json.dumps(default=str)). This test exercises exactly that real
    verification path, not a shortcut around it."""
    import hashlib
    import json

    from app.shared.dynamodb_types import decimal_to_native

    store = await _make_store()
    await _write(store, TENANT_A, "agent.created")

    event = decimal_to_native(_events_in_order(TENANT_A)[0])
    recomputed_input = {k: v for k, v in event.items() if k != "event_hash"}
    recomputed = hashlib.sha256(
        json.dumps(recomputed_input, sort_keys=True, default=str).encode()
    ).hexdigest()
    assert recomputed == event["event_hash"]


async def test_ensure_table_does_not_raise_when_called_repeatedly() -> None:
    """Idempotent re-creation (ResourceInUseException path) must not
    attempt to re-enable TTL/PITR — and must not raise either way."""
    store = await _make_store()
    await store.ensure_table()
    await store.ensure_table()
