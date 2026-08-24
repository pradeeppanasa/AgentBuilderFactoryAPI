"""Shared test fixtures.

All DynamoDB / Secrets Manager access goes through boto3, mocked here via
moto's in-memory AWS backend — no Docker services required. User accounts
(Phase 3) go through SQLAlchemy; tests point at a temp-file SQLite database
instead of real Postgres, sharing the same ORM models/migrations-equivalent
schema (`Base.metadata.create_all`) so the app code under test is identical
to what runs against Postgres in prototype/enterprise.

DATABASE_URL and JWT_SECRET_ARN are set at module import time (before any
`app.*` module — and therefore the `Settings()` singleton — is imported),
since pydantic-settings reads the environment once, at construction.
"""

from __future__ import annotations

import os
import tempfile
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from typing import Any

import pytest
from moto import mock_aws

_TMP_DB_DIR = tempfile.mkdtemp(prefix="panasa-test-db-")
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{_TMP_DB_DIR}/test.db"
# Forced empty, not just left alone: a developer's real .env (e.g. from
# scripts/local-setup.sh) sets DYNAMODB_ENDPOINT/SECRETS_MANAGER_ENDPOINT/
# S3_ENDPOINT to point at a real local Docker stack. pydantic-settings reads
# .env directly regardless of os.environ, so without this override the app
# would silently redirect boto3 at that REAL stack instead of moto's mocks —
# reproduced: it fetches the real LocalStack "jwt-secret" (a different value
# than TEST_JWT_SECRET below), and every JWT signature check then fails with
# 401. `settings.*_endpoint: str | None` treats "" as falsy, same as None,
# so the app's client factories skip adding a custom endpoint_url entirely
# and boto3 hits moto's normally-intercepted default AWS hostnames.
os.environ["DYNAMODB_ENDPOINT"] = ""
os.environ["SECRETS_MANAGER_ENDPOINT"] = ""
os.environ["S3_ENDPOINT"] = ""
# Same reasoning: a real .env sets LANGFUSE_HOST=http://langfuse:3000 (a
# Docker-internal-only hostname). Left alone, check_observability() makes a
# real httpx call that hangs until ConnectTimeout instead of resolving to
# "disabled" — reproduced in test_health.py, which asserts "disabled".
os.environ["LANGFUSE_HOST"] = ""
# Same reasoning again: a developer's real .env may set this true for their
# own local generate-iac testing (R46). Tests that want it enabled do so
# explicitly via monkeypatch — the baseline must be deterministic regardless
# of whatever a developer's own .env happens to have — reproduced: without
# this, test_non_local_validation_mode_forbidden_by_default failed against a
# real .env with the flag left on from manual testing.
os.environ["DEV_VALIDATION_EXTENDED_MODES_ENABLED"] = "false"
# Same reasoning again: a developer's real .env may set this true for their
# own manual Runs-feature testing (Observability — Runs Feature, Phase 1).
# Reproduced: test_seed_demo_forbidden_by_default failed against a real
# .env with SEED_RUNS_ENABLED=true left on from manual testing.
os.environ["SEED_RUNS_ENABLED"] = "false"
os.environ.setdefault("JWT_SECRET_ARN", "jwt-secret")
os.environ.setdefault("IAC_OUTPUT_BUCKET", "panasa-iac-artifacts-test")
os.environ.setdefault("AUDIT_S3_BUCKET", "panasa-audit-test")
os.environ.setdefault("GIT_PROVIDER", "github")
os.environ.setdefault("GIT_CREDENTIALS_SECRET", "git-token")
os.environ.setdefault("GIT_REPO_URL", "https://github.com/test-org/test-repo")
os.environ.setdefault("GIT_ORG", "test-org")
os.environ.setdefault("EVENTBRIDGE_BUS_NAME", "panasa-agent-builder-test")
# Deliberately unreachable (connection refused, not a timeout) so
# check_cache()'s "error" path is deterministic in tests regardless of
# whether a real Redis happens to be running on the test machine — see
# tests/test_platform_health.py for the "ok" path, exercised against
# fakeredis directly instead.
os.environ.setdefault("REDIS_URL", "redis://127.0.0.1:1/0")

TEST_JWT_SECRET = "test-jwt-signing-secret-not-for-production"
TEST_GIT_TOKEN = "test-git-token-not-for-production"  # noqa: S105
TEST_IAC_BUCKET = os.environ["IAC_OUTPUT_BUCKET"]
TEST_AUDIT_BUCKET = os.environ["AUDIT_S3_BUCKET"]
TEST_EVENTBRIDGE_BUS = os.environ["EVENTBRIDGE_BUS_NAME"]


@pytest.fixture(autouse=True)
def aws_credentials() -> None:
    os.environ.setdefault("AWS_ACCESS_KEY_ID", "testing")
    os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "testing")
    os.environ.setdefault("AWS_SECURITY_TOKEN", "testing")
    os.environ.setdefault("AWS_SESSION_TOKEN", "testing")
    os.environ.setdefault("AWS_DEFAULT_REGION", "eu-west-2")


@pytest.fixture(autouse=True)
def mocked_aws(aws_credentials: None) -> Iterator[None]:
    with mock_aws():
        import boto3

        secretsmanager = boto3.client("secretsmanager", region_name="eu-west-2")
        secretsmanager.create_secret(Name="jwt-secret", SecretString=TEST_JWT_SECRET)
        secretsmanager.create_secret(Name="git-token", SecretString=TEST_GIT_TOKEN)

        s3 = boto3.client("s3", region_name="eu-west-2")
        s3.create_bucket(
            Bucket=TEST_IAC_BUCKET,
            CreateBucketConfiguration={"LocationConstraint": "eu-west-2"},
        )
        s3.create_bucket(
            Bucket=TEST_AUDIT_BUCKET,
            CreateBucketConfiguration={"LocationConstraint": "eu-west-2"},
        )
        boto3.client("events", region_name="eu-west-2").create_event_bus(Name=TEST_EVENTBRIDGE_BUS)
        yield


@pytest.fixture(autouse=True)
async def reset_database() -> AsyncIterator[None]:
    from sqlalchemy.ext.asyncio import create_async_engine

    from app.modules.auth.models import Base

    engine = create_async_engine(os.environ["DATABASE_URL"])
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.drop_all)
        await connection.run_sync(Base.metadata.create_all)
    await engine.dispose()
    yield


@pytest.fixture
async def make_user_and_token() -> Callable[..., Awaitable[tuple[Any, str]]]:
    from app.config import settings
    from app.modules.auth.db import create_db_engine, create_session_factory
    from app.modules.auth.models import User
    from app.modules.auth.security import create_access_token, hash_password

    async def _make(
        tenant_id: str,
        role: str = "developer",
        email: str | None = None,
        password: str = "TestPassword123!",
        is_active: bool = True,
    ) -> tuple[User, str]:
        engine = create_db_engine(settings)
        session_factory = create_session_factory(engine)
        user = User(
            email=email or f"{uuid.uuid4().hex}@example.com",
            hashed_password=hash_password(password),
            role=role,
            tenant_id=tenant_id,
            is_active=is_active,
        )
        async with session_factory() as session:
            session.add(user)
            await session.commit()
            await session.refresh(user)
        await engine.dispose()

        token = create_access_token(
            user,
            TEST_JWT_SECRET,
            settings.jwt_algorithm,
            settings.jwt_access_token_expire_minutes,
        )
        return user, token

    return _make


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}
