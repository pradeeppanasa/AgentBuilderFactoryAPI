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
    dynamodb = boto3.resource("dynamodb", region_name="eu-west-2")
    dynamodb.create_table(
        TableName=settings.dynamodb_agents_table,
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

    assert await check_database(dynamodb, settings) == "ok"


async def test_check_database_error_when_table_missing() -> None:
    dynamodb = boto3.resource("dynamodb", region_name="eu-west-2")
    assert await check_database(dynamodb, settings) == "error"


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
    stub_settings = settings.model_copy(update={"langfuse_host": "http://127.0.0.1:1"})
    assert await check_observability(stub_settings) == "error"


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
    stub_settings = settings.model_copy(update={"langfuse_host": "http://langfuse:3000"})
    assert await check_observability(stub_settings) == "ok"
