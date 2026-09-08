"""Unit tests for quota.py (Sprint 3 Phase 9 — S-03, R70, CLAUDE.md
Section 67)."""

from __future__ import annotations

import time
from typing import Any

import boto3
import fakeredis
import pytest
from moto import mock_aws
from quota import check_monthly_budget, check_rate_limit, record_llm_cost


@pytest.fixture
def redis_client() -> fakeredis.FakeAsyncRedis:
    return fakeredis.FakeAsyncRedis()


# ── check_rate_limit ─────────────────────────────────────────────────────


async def test_rpm_within_limit_is_allowed(redis_client: fakeredis.FakeAsyncRedis) -> None:
    for _ in range(5):
        allowed, reason = await check_rate_limit(
            "tenant-a", "agent-1", rpm_limit=5, rpd_limit=None, r=redis_client
        )
        assert allowed is True
        assert reason is None


async def test_rpm_over_limit_is_denied(redis_client: fakeredis.FakeAsyncRedis) -> None:
    for _ in range(3):
        allowed, _ = await check_rate_limit(
            "tenant-a", "agent-1", rpm_limit=3, rpd_limit=None, r=redis_client
        )
        assert allowed is True

    allowed, reason = await check_rate_limit(
        "tenant-a", "agent-1", rpm_limit=3, rpd_limit=None, r=redis_client
    )
    assert allowed is False
    assert reason == "rpm_exceeded"


async def test_rpd_over_limit_is_denied_even_when_rpm_has_headroom(
    redis_client: fakeredis.FakeAsyncRedis,
) -> None:
    for _ in range(2):
        allowed, _ = await check_rate_limit(
            "tenant-a", "agent-1", rpm_limit=1000, rpd_limit=2, r=redis_client
        )
        assert allowed is True

    allowed, reason = await check_rate_limit(
        "tenant-a", "agent-1", rpm_limit=1000, rpd_limit=2, r=redis_client
    )
    assert allowed is False
    assert reason == "rpd_exceeded"


async def test_none_limit_is_skipped_not_treated_as_zero(
    redis_client: fakeredis.FakeAsyncRedis,
) -> None:
    for _ in range(100):
        allowed, _ = await check_rate_limit(
            "tenant-a", "agent-1", rpm_limit=None, rpd_limit=None, r=redis_client
        )
        assert allowed is True


async def test_different_tenants_or_agents_have_independent_budgets(
    redis_client: fakeredis.FakeAsyncRedis,
) -> None:
    for _ in range(3):
        allowed, _ = await check_rate_limit(
            "tenant-a", "agent-1", rpm_limit=3, rpd_limit=None, r=redis_client
        )
        assert allowed is True
    allowed, _ = await check_rate_limit(
        "tenant-a", "agent-1", rpm_limit=3, rpd_limit=None, r=redis_client
    )
    assert allowed is False

    # Same agent_id, different tenant — untouched by tenant-a's usage.
    allowed, _ = await check_rate_limit(
        "tenant-b", "agent-1", rpm_limit=3, rpd_limit=None, r=redis_client
    )
    assert allowed is True


async def test_ttl_is_set_on_first_increment_only(redis_client: fakeredis.FakeAsyncRedis) -> None:
    await check_rate_limit("tenant-a", "agent-2", rpm_limit=10, rpd_limit=None, r=redis_client)
    keys = await redis_client.keys("ratelimit:rpm:tenant-a:agent-2:*")
    assert len(keys) == 1
    ttl = await redis_client.ttl(keys[0])
    assert 0 < ttl <= 61


class _ExplodingRedis:
    """Simulates Redis being unreachable — every call raises."""

    async def incr(self, key: str) -> int:
        raise ConnectionError("redis unavailable")


async def test_fails_open_when_redis_unreachable() -> None:
    allowed, reason = await check_rate_limit(
        "tenant-a",
        "agent-1",
        rpm_limit=1,
        rpd_limit=None,
        r=_ExplodingRedis(),  # type: ignore[arg-type]
    )
    assert allowed is True
    assert reason is None


# ── check_monthly_budget ─────────────────────────────────────────────────


def _current_period() -> str:
    return time.strftime("%Y-%m", time.gmtime())


def test_no_budget_configured_always_allows() -> None:
    assert check_monthly_budget({}, monthly_budget_usd=None) is True


def test_no_spend_recorded_yet_is_within_budget() -> None:
    assert check_monthly_budget({}, monthly_budget_usd=10.0) is True


def test_spend_within_budget_for_current_period_is_allowed() -> None:
    record = {"current_month_spend_usd": 5.0, "current_month_spend_period": _current_period()}
    assert check_monthly_budget(record, monthly_budget_usd=10.0) is True


def test_spend_over_budget_for_current_period_is_denied() -> None:
    record = {"current_month_spend_usd": 15.0, "current_month_spend_period": _current_period()}
    assert check_monthly_budget(record, monthly_budget_usd=10.0) is False


def test_spend_from_a_previous_period_does_not_count_against_current_budget() -> None:
    """Lazy monthly reset — a stale period's spend shouldn't block a
    brand-new month even before record_llm_cost() has run once to reset
    it for real."""
    record = {"current_month_spend_usd": 999.0, "current_month_spend_period": "2020-01"}
    assert check_monthly_budget(record, monthly_budget_usd=10.0) is True


# ── record_llm_cost ──────────────────────────────────────────────────────


@pytest.fixture
def agents_table() -> Any:
    with mock_aws():
        dynamodb = boto3.resource("dynamodb", region_name="eu-west-2")
        table = dynamodb.create_table(
            TableName="panasa-agents",
            KeySchema=[
                {"AttributeName": "tenant_id", "KeyType": "HASH"},
                {"AttributeName": "agent_id", "KeyType": "RANGE"},
            ],
            AttributeDefinitions=[
                {"AttributeName": "tenant_id", "AttributeType": "S"},
                {"AttributeName": "agent_id", "AttributeType": "S"},
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        table.put_item(Item={"tenant_id": "tenant-a", "agent_id": "agent-1"})
        yield dynamodb


def test_record_llm_cost_sets_spend_on_first_call(agents_table: Any) -> None:
    record_llm_cost("tenant-a", "agent-1", 0.05, dynamodb=agents_table)

    item = agents_table.Table("panasa-agents").get_item(
        Key={"tenant_id": "tenant-a", "agent_id": "agent-1"}
    )["Item"]
    assert float(item["current_month_spend_usd"]) == pytest.approx(0.05)
    assert item["current_month_spend_period"] == _current_period()


def test_record_llm_cost_accumulates_within_the_same_period(agents_table: Any) -> None:
    record_llm_cost("tenant-a", "agent-1", 0.05, dynamodb=agents_table)
    record_llm_cost("tenant-a", "agent-1", 0.03, dynamodb=agents_table)

    item = agents_table.Table("panasa-agents").get_item(
        Key={"tenant_id": "tenant-a", "agent_id": "agent-1"}
    )["Item"]
    assert float(item["current_month_spend_usd"]) == pytest.approx(0.08)


def test_record_llm_cost_resets_when_period_has_rolled_over(agents_table: Any) -> None:
    agents_table.Table("panasa-agents").update_item(
        Key={"tenant_id": "tenant-a", "agent_id": "agent-1"},
        UpdateExpression=(
            "SET current_month_spend_usd = :cost, current_month_spend_period = :period"
        ),
        ExpressionAttributeValues={":cost": 999, ":period": "2020-01"},
    )

    record_llm_cost("tenant-a", "agent-1", 0.10, dynamodb=agents_table)

    item = agents_table.Table("panasa-agents").get_item(
        Key={"tenant_id": "tenant-a", "agent_id": "agent-1"}
    )["Item"]
    assert float(item["current_month_spend_usd"]) == pytest.approx(0.10)
    assert item["current_month_spend_period"] == _current_period()


def test_record_llm_cost_is_a_noop_for_none_or_zero_cost(agents_table: Any) -> None:
    record_llm_cost("tenant-a", "agent-1", None, dynamodb=agents_table)
    record_llm_cost("tenant-a", "agent-1", 0.0, dynamodb=agents_table)

    item = agents_table.Table("panasa-agents").get_item(
        Key={"tenant_id": "tenant-a", "agent_id": "agent-1"}
    )["Item"]
    assert "current_month_spend_usd" not in item


def test_record_llm_cost_never_raises_when_dynamodb_fails() -> None:
    class _ExplodingResource:
        def Table(self, name: str) -> Any:
            raise RuntimeError("DynamoDB is down")

    record_llm_cost("tenant-a", "agent-1", 0.05, dynamodb=_ExplodingResource())
