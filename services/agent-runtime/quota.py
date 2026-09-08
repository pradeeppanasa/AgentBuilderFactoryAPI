"""Rate limiting + monthly cost budget enforcement (Sprint 3 Phase 9 — S-03,
R70, CLAUDE.md Section 67).

Two different mechanisms for two different kinds of limit, deliberately not
unified into one:

  rpm/rpd  -> Redis, atomic INCR, explicit TTL, FAILS OPEN on Redis error
              (R29/R39 — rate limiting is the one control this codebase
              allows to fail open; it protects against abuse/cost spikes,
              it is not an identity/access control).
  monthly
  budget   -> DynamoDB, atomic ADD from real per-request litellm cost
              (never Redis — R29: Redis is never the source of truth for
              durable data), FAILS CLOSED if the check can't be performed
              — an inability to determine current spend must never
              silently allow unbounded cost.

Adapted from app/middleware/rate_limit.py's rpm-only sketch (Factory
Runtime, dead/unwired code there per Section 67's own gap analysis — and
unimportable here regardless, F8: zero shared code between the two
services) to match Section 67.2's corrected, tenant+agent-scoped, rpm+rpd
design exactly.
"""

from __future__ import annotations

import os
import time
from decimal import Decimal
from typing import Any

import redis.asyncio as redis
import structlog
from botocore.exceptions import ClientError

logger = structlog.get_logger()

_RPM_WINDOW_TTL_SECONDS = 61  # a little longer than the 60s window it covers
_RPD_WINDOW_TTL_SECONDS = 90_000  # a little longer than the 86400s window it covers


async def check_rate_limit(
    tenant_id: str,
    agent_id: str,
    rpm_limit: int | None,
    rpd_limit: int | None,
    r: redis.Redis,
) -> tuple[bool, str | None]:
    """Returns (allowed, reason_if_denied). A None limit means that window
    isn't configured for this agent — skipped, not treated as unlimited-
    but-still-counted."""
    now = time.time()
    checks = [
        (
            rpm_limit,
            f"ratelimit:rpm:{tenant_id}:{agent_id}:{int(now // 60)}",
            _RPM_WINDOW_TTL_SECONDS,
            "rpm",
        ),
        (
            rpd_limit,
            f"ratelimit:rpd:{tenant_id}:{agent_id}:{int(now // 86400)}",
            _RPD_WINDOW_TTL_SECONDS,
            "rpd",
        ),
    ]
    try:
        for limit, key, ttl, label in checks:
            if limit is None:
                continue
            count = await r.incr(key)
            if count == 1:
                await r.expire(key, ttl)
            if count > limit:
                return False, f"{label}_exceeded"
        return True, None
    except Exception:
        logger.warning(
            "quota.redis_unavailable_failing_open", tenant_id=tenant_id, agent_id=agent_id
        )
        return True, None  # fail open — rate limiting only, R29/R39


def _current_period() -> str:
    return time.strftime("%Y-%m", time.gmtime())


def check_monthly_budget(agent_record: dict[str, Any], monthly_budget_usd: float | None) -> bool:
    """Returns True if the agent is still within its configured monthly
    budget. `agent_record` should be a freshly-fetched panasa-agents item
    (main.py's auth_middleware already fetches one per request for the
    R67 tenant check — reuse it, don't re-fetch).

    Enforcement is necessarily soft, not a hard pre-block: a request's own
    real cost is only known after the LLM call returns (true of every
    LLM cost-tracking system, not specific to this platform — see
    AgentRecord.current_month_spend_usd's own docstring, Factory Runtime).
    This can deny requests AFTER the one that pushed spend over budget,
    never that one itself.
    """
    if monthly_budget_usd is None:
        return True
    period = agent_record.get("current_month_spend_period")
    if period != _current_period():
        # Nothing recorded against the current period yet.
        return True
    spend = agent_record.get("current_month_spend_usd") or 0
    return float(spend) <= monthly_budget_usd


def record_llm_cost(
    tenant_id: str,
    agent_id: str,
    cost_usd: float | None,
    dynamodb: Any | None = None,
) -> None:
    """Atomically adds this request's real litellm cost to the agent's
    durable running total. Lazily resets the total to just this request's
    own cost the first time it sees a period that doesn't match
    current_month_spend_period, rather than requiring a separate
    scheduled reset job (Section 67.3 — out of this phase's scope). See
    AgentRecord.current_month_spend_period's own docstring (Factory
    Runtime, app/modules/registry/models.py) for the accepted, explicitly
    documented month-boundary race this implies.

    cost_usd is None whenever litellm couldn't compute a cost for this
    call (llm_client.py's own documented behaviour, e.g. an unsupported
    model) — silently a no-op rather than tracking a fabricated $0, since
    "unknown" and "definitely zero" are different things and conflating
    them would make the budget check look more accurate than it is.

    Best-effort: a failure here must never fail the /chat response the
    caller is already waiting on (the LLM call already completed and the
    user already has their answer) — logged, never raised.
    """
    if cost_usd is None or cost_usd <= 0:
        return

    period = _current_period()
    try:
        import boto3

        region = os.environ.get("AWS_REGION", "eu-west-2")
        agents_table_name = os.environ.get("DYNAMODB_AGENTS_TABLE", "panasa-agents")
        resource = dynamodb or boto3.resource("dynamodb", region_name=region)
        agents_table = resource.Table(agents_table_name)

        try:
            agents_table.update_item(
                Key={"tenant_id": tenant_id, "agent_id": agent_id},
                UpdateExpression="ADD current_month_spend_usd :cost",
                ConditionExpression="current_month_spend_period = :period",
                ExpressionAttributeValues={
                    ":cost": Decimal(str(cost_usd)),
                    ":period": period,
                },
            )
        except ClientError as exc:
            if exc.response["Error"]["Code"] != "ConditionalCheckFailedException":
                raise
            # Either the very first cost this agent has ever recorded, or
            # the period just rolled over — start the new period fresh
            # rather than accumulating across month boundaries forever.
            agents_table.update_item(
                Key={"tenant_id": tenant_id, "agent_id": agent_id},
                UpdateExpression=(
                    "SET current_month_spend_usd = :cost, current_month_spend_period = :period"
                ),
                ExpressionAttributeValues={
                    ":cost": Decimal(str(cost_usd)),
                    ":period": period,
                },
            )
    except Exception:
        logger.warning(
            "quota.spend_tracking_failed", tenant_id=tenant_id, agent_id=agent_id, exc_info=True
        )


def get_redis_client() -> redis.Redis:
    """Constructed once at module scope by main.py, matching this
    service's existing no-DI-framework convention (config_loader.py's
    module-level `agent_config = load_agent_config()`)."""
    url = os.environ.get("REDIS_URL", "redis://localhost:6379/0")
    return redis.Redis.from_url(url, decode_responses=True, socket_timeout=1.0)
