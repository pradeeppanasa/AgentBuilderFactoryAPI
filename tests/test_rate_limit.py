"""Unit tests for app.middleware.rate_limit (CLAUDE.md Section 32.2, R29/R39)."""

from __future__ import annotations

import fakeredis
import pytest

from app.middleware.rate_limit import check_rate_limit


@pytest.fixture
def redis_client() -> fakeredis.FakeAsyncRedis:
    return fakeredis.FakeAsyncRedis()


async def test_requests_within_limit_are_allowed(redis_client: fakeredis.FakeAsyncRedis) -> None:
    for _ in range(5):
        assert await check_rate_limit("agent-1", rpm_limit=5, r=redis_client) is True


async def test_request_over_limit_is_denied(redis_client: fakeredis.FakeAsyncRedis) -> None:
    for _ in range(3):
        assert await check_rate_limit("agent-1", rpm_limit=3, r=redis_client) is True

    assert await check_rate_limit("agent-1", rpm_limit=3, r=redis_client) is False


async def test_ttl_is_set_on_first_increment_only(redis_client: fakeredis.FakeAsyncRedis) -> None:
    await check_rate_limit("agent-2", rpm_limit=10, r=redis_client)
    keys = await redis_client.keys("ratelimit:agent-2:*")
    assert len(keys) == 1
    ttl = await redis_client.ttl(keys[0])
    assert 0 < ttl <= 61


async def test_different_agents_have_independent_budgets(
    redis_client: fakeredis.FakeAsyncRedis,
) -> None:
    for _ in range(3):
        assert await check_rate_limit("agent-a", rpm_limit=3, r=redis_client) is True
    assert await check_rate_limit("agent-a", rpm_limit=3, r=redis_client) is False

    # agent-b's budget is untouched by agent-a's usage.
    assert await check_rate_limit("agent-b", rpm_limit=3, r=redis_client) is True


class _ExplodingRedis:
    """Simulates Redis being unreachable — every call raises."""

    async def incr(self, key: str) -> int:
        raise ConnectionError("redis unavailable")


async def test_fails_open_when_redis_unreachable() -> None:
    result = await check_rate_limit("agent-1", rpm_limit=1, r=_ExplodingRedis())  # type: ignore[arg-type]
    assert result is True
