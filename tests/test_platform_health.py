"""Unit tests for app.modules.platform.health (CLAUDE.md Section 32.4).

database/storage go through moto (already wired up by conftest.py's
autouse mocked_aws fixture — same DynamoDB/S3 backends the rest of the
suite uses). cache uses fakeredis directly since there's no moto-equivalent
for Redis. observability is exercised against both the "disabled" (no
langfuse_host) and "ok"/"error" (real httpx call) paths.
"""

from __future__ import annotations

import boto3
import fakeredis
import pytest

from app.config import settings
from app.modules.platform.health import (
    check_cache,
    check_database,
    check_model_router,
    check_observability,
    check_storage,
)


async def test_check_database_ok_when_table_exists() -> None:
    # Perf refactor (2026-09-08): conftest.py's mocked_aws is now
    # session-scoped, so a hand-rolled create_table with a schema that
    # diverges from the real one (this used to omit AgentRegistryStore's
    # project-index GSI) would either raise ResourceInUseException once
    # some other test has already created the real table, or — if this
    # test happened to run first — permanently poison the shared table's
    # schema for the rest of the session. Using the real store's own
    # ensure_tables() (same pattern as test_policy_enforcement.py) is both
    # correct and already idempotent (ResourceInUseException-safe).
    from app.modules.registry.store import AgentRegistryStore

    dynamodb = boto3.resource("dynamodb", region_name="eu-west-2")
    await AgentRegistryStore(dynamodb, settings).ensure_tables()

    assert await check_database(dynamodb, settings) == "ok"


async def test_check_database_error_when_table_missing() -> None:
    # Perf refactor (2026-09-08): the real agents table now persists for
    # the whole session (almost certainly already created by an earlier
    # test), so "missing" can no longer be exercised via the shared table
    # name — point check_database at a name guaranteed never to exist
    # instead of relying on settings.dynamodb_agents_table being absent.
    dynamodb = boto3.resource("dynamodb", region_name="eu-west-2")
    missing_table_settings = settings.model_copy(
        update={"dynamodb_agents_table": "panasa-agents-table-that-does-not-exist"}
    )
    assert await check_database(dynamodb, missing_table_settings) == "error"


async def test_check_storage_ok_when_bucket_exists() -> None:
    s3 = boto3.client("s3", region_name="eu-west-2")
    # settings.iac_output_bucket is created by conftest.py's mocked_aws fixture.
    assert await check_storage(s3, settings) == "ok"


async def test_check_storage_error_when_bucket_missing() -> None:
    s3 = boto3.client("s3", region_name="eu-west-2")
    stub_settings = settings.model_copy(update={"iac_output_bucket": "does-not-exist-bucket"})
    assert await check_storage(s3, stub_settings) == "error"


async def test_check_storage_error_when_bucket_unset() -> None:
    s3 = boto3.client("s3", region_name="eu-west-2")
    stub_settings = settings.model_copy(update={"iac_output_bucket": None})
    assert await check_storage(s3, stub_settings) == "error"


async def test_check_cache_ok_against_fakeredis() -> None:
    fake = fakeredis.FakeAsyncRedis()
    assert await check_cache(fake) == "ok"


async def test_check_cache_error_when_unreachable() -> None:
    import redis.asyncio as redis

    unreachable = redis.Redis.from_url("redis://127.0.0.1:1/0", socket_timeout=1.0)
    assert await check_cache(unreachable) == "error"


def test_check_model_router_ok() -> None:
    assert check_model_router() == "ok"


async def test_check_observability_disabled_when_unconfigured() -> None:
    stub_settings = settings.model_copy(update={"langfuse_host": None})
    assert await check_observability(stub_settings) == "disabled"


async def test_check_observability_error_when_unreachable() -> None:
    stub_settings = settings.model_copy(
        update={"langfuse_enabled": True, "langfuse_host": "http://127.0.0.1:1"}
    )
    assert await check_observability(stub_settings) == "error"


async def test_check_observability_disabled_when_host_set_but_flag_off() -> None:
    """R45: Langfuse is optional and off by default — a host left over from
    a previous config must not make the health check attempt a connection
    unless langfuse_enabled is explicitly True."""
    stub_settings = settings.model_copy(
        update={"langfuse_enabled": False, "langfuse_host": "http://langfuse:3000"}
    )
    assert await check_observability(stub_settings) == "disabled"


async def test_check_observability_ok_when_reachable(monkeypatch: pytest.MonkeyPatch) -> None:
    import httpx

    class _FakeAsyncClient:
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

        async def __aenter__(self) -> _FakeAsyncClient:
            return self

        async def __aexit__(self, *exc_info: object) -> None:
            return None

        async def get(self, url: str) -> None:
            return None

    monkeypatch.setattr(httpx, "AsyncClient", _FakeAsyncClient)
    stub_settings = settings.model_copy(
        update={"langfuse_enabled": True, "langfuse_host": "http://langfuse:3000"}
    )
    assert await check_observability(stub_settings) == "ok"
