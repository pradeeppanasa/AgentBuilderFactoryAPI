"""Per-service health checks (CLAUDE.md Section 32.4).

Each check is a short, cheap reachability probe with its own try/except —
one dependency being down must never take the whole health endpoint down
with it, and none of these may invoke an LLM or spend tokens (model_router's
check is importability only, never a real completion call).
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx
import litellm
import redis.asyncio as redis

from app.config import Settings

logger = logging.getLogger(__name__)

_OBSERVABILITY_TIMEOUT_SECONDS = 2.0


async def check_database(dynamodb_resource: Any, settings: Settings) -> str:
    try:
        await asyncio.to_thread(
            dynamodb_resource.meta.client.describe_table,
            TableName=settings.dynamodb_agents_table,
        )
        return "ok"
    except Exception:
        logger.warning("health.database.unreachable", exc_info=True)
        return "error"


async def check_storage(s3_client: Any, settings: Settings) -> str:
    if not settings.iac_output_bucket:
        return "error"
    try:
        await asyncio.to_thread(s3_client.head_bucket, Bucket=settings.iac_output_bucket)
        return "ok"
    except Exception:
        logger.warning("health.storage.unreachable", exc_info=True)
        return "error"


async def check_cache(redis_client: redis.Redis) -> str:
    try:
        await redis_client.ping()
        return "ok"
    except Exception:
        logger.warning("health.cache.unreachable", exc_info=True)
        return "error"


def check_model_router() -> str:
    """Importability + presence of the acompletion entrypoint only — never
    an LLM call (Section 32.4)."""
    return "ok" if hasattr(litellm, "acompletion") else "error"


async def check_observability(settings: Settings) -> str:
    if not settings.langfuse_enabled or not settings.langfuse_host:
        return "disabled"
    try:
        async with httpx.AsyncClient(timeout=_OBSERVABILITY_TIMEOUT_SECONDS) as client:
            await client.get(settings.langfuse_host)
        return "ok"
    except Exception:
        logger.warning("health.observability.unreachable", exc_info=True)
        return "error"
