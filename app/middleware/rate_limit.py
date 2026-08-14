"""Redis-backed rate limiting (CLAUDE.md Section 32.2, R29/R39).

Enforces AgentConfiguration.rate_limit_rpm using an atomic per-minute
counter — INCR both creates and increments the key in one round trip, so
there's no read-then-write race between concurrent requests for the same
agent. The TTL is set only on the first increment of each window (count==1);
every write here has an explicit expiry (R29 — Redis is a cache, never the
source of truth, and nothing here relies on maxmemory-policy eviction as a
substitute for that).

R39: rate limiting is the ONE thing allowed to fail open when Redis is
unreachable — it is not a security control. Authentication, authorization,
licensing, tenant isolation, guardrails, and policy enforcement must never
adopt this pattern; they fail closed.
"""

from __future__ import annotations

import logging
import time

import redis.asyncio as redis

logger = logging.getLogger(__name__)

_WINDOW_SECONDS = 60
_WINDOW_TTL_SECONDS = 61  # a little longer than the window so a slow write can't race the expiry


async def check_rate_limit(agent_id: str, rpm_limit: int, r: redis.Redis) -> bool:
    """Returns True if the request is allowed, False if the agent is over
    its per-minute budget. Fails open (returns True) if Redis itself is
    unavailable — see module docstring for why that's safe here and nowhere
    else."""
    try:
        window = int(time.time() // _WINDOW_SECONDS)
        key = f"ratelimit:{agent_id}:{window}"
        count = await r.incr(key)
        if count == 1:
            await r.expire(key, _WINDOW_TTL_SECONDS)
        return count <= rpm_limit
    except Exception:
        logger.warning("redis unavailable for rate limit check on %s — failing open", agent_id)
        return True
