"""Redis client factory (CLAUDE.md Section 32.2).

Same shape as app.shared.dynamodb / app.shared.s3: created once at app
startup (see app/main.py lifespan) and handed to modules via app.state, not
module-level caching, so tests can inject an isolated (fake) client per test.

Redis is a cache layer only (R29) — never the source of truth for durable
data. socket_timeout is short and finite so a stalled cache never blocks a
request; callers that read/write Redis must handle it being unreachable
themselves (see app.middleware.rate_limit's fail-open, scoped to rate
limiting only per R39).
"""

from __future__ import annotations

import redis.asyncio as redis

from app.config import Settings


def create_redis_client(settings: Settings) -> redis.Redis:
    pool = redis.ConnectionPool.from_url(
        settings.redis_url,
        max_connections=settings.redis_max_connections,
        socket_timeout=settings.redis_socket_timeout,
        decode_responses=True,
    )
    return redis.Redis(connection_pool=pool)
