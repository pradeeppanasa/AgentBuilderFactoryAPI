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
# S3_ENDPOINT/EVENTBRIDGE_ENDPOINT to point at a real local Docker stack.
# pydantic-settings reads .env directly regardless of os.environ, so
# without this override the app would silently redirect boto3 at that REAL
# stack instead of moto's mocks — reproduced: it fetches the real
# LocalStack "jwt-secret" (a different value than TEST_JWT_SECRET below),
# and every JWT signature check then fails with 401.
# `settings.*_endpoint: str | None` treats "" as falsy, same as None, so
# the app's client factories skip adding a custom endpoint_url entirely
# and boto3 hits moto's normally-intercepted default AWS hostnames.
#
# EVENTBRIDGE_ENDPOINT was missing from this list until it was caught here:
# every deploy-triggering test (test_deploy_api.py, test_deployment_
# approval_api.py, test_phase17_e2e_scenario.py, ...) goes through
# DeploymentOrchestrator.trigger_deployment()'s real EventBridge
# put_events call, which was silently hitting the real LocalStack
# container on :4566 instead of moto — each such test took as long as a
# real network round-trip to a container that's been running under heavy
# manual-testing load for hours, rather than the ~1s an in-memory mock
# takes. Reproduced: a `netstat`-equivalent on the hung pytest process
# showed an ESTABLISHED connection to ::1:4566 mid-test.
os.environ["DYNAMODB_ENDPOINT"] = ""
os.environ["SECRETS_MANAGER_ENDPOINT"] = ""
os.environ["S3_ENDPOINT"] = ""
os.environ["EVENTBRIDGE_ENDPOINT"] = ""
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
# Same reasoning again: a developer's real .env may set this true (plus real
# absolute terraform/tfsec/checkov paths) for their own manual deployment-
# pipeline testing. Left alone, every test that triggers a deploy without
# swapping app.state.deployment_pipeline_simulator (most of them — only
# test_deployment_pipeline_simulator.py does) picks up the real,
# app-startup-constructed simulator and kicks off a genuine, slow,
# network-dependent terraform/tfsec/checkov run per deploy — reproduced:
# the full suite hung for minutes on a real `terraform init` spawned from a
# background pytest run. Tests that want it enabled do so explicitly via
# monkeypatch, same pattern as DEV_VALIDATION_EXTENDED_MODES_ENABLED above.
os.environ["SIMULATE_DEPLOYMENT_PIPELINE"] = "false"
# That fix above was incomplete: app.state.iac_validator (app/main.py) is
# built from settings.terraform_binary_path unconditionally at startup —
# not gated by SIMULATE_DEPLOYMENT_PIPELINE at all — and IaCScanRunner reads
# settings.tfsec_binary_path/checkov_python_path the same way. Any test that
# exercises either (e.g. POST /agents/{id}/generate-iac, which always runs
# the real IaCValidator per its own docstring) still picked up a developer's
# real absolute tool paths from .env and shelled out for real — reproduced:
# with only the override above, the full suite still spawned a real
# `terraform.exe` (the real Winget-installed absolute path from .env) against
# real generated Terraform (real provider blocks -> `terraform init` tries to
# fetch the AWS provider plugin from the network) and hung for minutes.
#
# Deliberately NOT the plain "terraform"/"tfsec" command names: on a dev
# machine that has either genuinely on PATH (as this one does — installed
# earlier in this session for manual testing), that would still shell out
# for real, just non-deterministically depending on what happens to be
# installed. A binary name guaranteed not to exist anywhere forces
# subprocess.run's FileNotFoundError every time, which is exactly what
# validator.py's/iac_scan_runner.py's own "tool not found -> skip,
# passed=True" fallback is designed to handle — the automated suite's
# baseline must be deterministic regardless of the dev machine's tool
# installs, same rationale as every override above.
os.environ["TERRAFORM_BINARY_PATH"] = "panasa-test-terraform-not-installed"
os.environ["TFSEC_BINARY_PATH"] = "panasa-test-tfsec-not-installed"
os.environ["CHECKOV_PYTHON_PATH"] = ""
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


@pytest.fixture(autouse=True, scope="session")
def aws_credentials() -> None:
    os.environ.setdefault("AWS_ACCESS_KEY_ID", "testing")
    os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "testing")
    os.environ.setdefault("AWS_SECURITY_TOKEN", "testing")
    os.environ.setdefault("AWS_SESSION_TOKEN", "testing")
    os.environ.setdefault("AWS_DEFAULT_REGION", "eu-west-2")


# Perf refactor (2026-09-08): mocked_aws used to be function-scoped — a
# brand new moto backend per test, meaning every one of the ~800 tests in
# this suite re-ran the FastAPI app's lifespan, which calls ensure_table()
# for ~20 DynamoDB tables, EVERY time. That table-creation cost (confirmed
# the dominant cost across the full suite: individual files run at
# ~1-2s/test, but the full run took 30-40 minutes) is now paid ONCE per
# pytest session instead of once per test. _reset_aws_state below restores
# the "every test starts from an empty slate" guarantee tests still need,
# without paying to recreate table/bucket *schema* every time.
_BASELINE_SECRET_NAMES = {"jwt-secret", "git-token"}


@pytest.fixture(autouse=True, scope="session")
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
async def _reset_aws_state(mocked_aws: None) -> None:
    """Runs before every test (function-scoped, unlike mocked_aws above) —
    wipes DATA, never SCHEMA: every DynamoDB table's items, every S3
    bucket's objects, and every Secrets Manager secret except the two
    conftest-owned baseline ones. Recreating schema (tables/buckets) is
    exactly the per-test cost the session-scoped mocked_aws fixture above
    exists to avoid; wiping only the data inside it is cheap (test data
    volumes are a handful of items/objects/secrets per test) and preserves
    every test's existing "I start from nothing" assumption.

    Secrets are force-deleted (ForceDeleteWithoutRecovery) so a name is
    immediately reusable next test — real AWS's default 7-30 day recovery
    window would otherwise block recreation with the same name, which
    tests like test_admin_settings_api.py's Langfuse/Datadog/New Relic
    integration-secret tests rely on being possible (they all create a
    secret under the same tenant-scoped name whenever `existing_arn` is
    None, which is every time under per-test-isolated state).

    One deliberate exception to "wipe everything": ToolRegistryStore
    (Sprint 3 Phase 7, S-13a) seeds panasa-tool-registry with 4 APPROVED
    tools ONLY on the run that actually creates the table — by design, so
    an operator's later edits to a seeded row are never silently reset on
    restart (see that store's own docstring). Under session-scoped
    mocked_aws the table is created exactly once, by whichever test runs
    first; a blind item-wipe here would delete those 4 seed rows on every
    test AFTER that one and never restore them (ensure_table() sees the
    table already exists and correctly skips reseeding, same as it would
    against a real, already-provisioned AWS account) — every agent-config
    test that references jira_search/kb_search/etc. would then fail
    AgentConfigValidator's registry check. Re-seed it every time instead
    of excluding it from the wipe, so its content stays identical to a
    freshly-created table on every test, matching what tests got for free
    under the old per-test-fresh-backend design.
    """
    import boto3

    from app.config import settings
    from app.modules.tool_registry.store import ToolRegistryStore

    dynamodb = boto3.resource("dynamodb", region_name="eu-west-2")
    tool_registry_table_wiped = False
    for table in dynamodb.tables.all():
        key_names = [k["AttributeName"] for k in table.key_schema]
        with table.batch_writer() as batch:
            for item in table.scan().get("Items", []):
                batch.delete_item(Key={k: item[k] for k in key_names})
        if table.name == settings.dynamodb_tool_registry_table:
            tool_registry_table_wiped = True

    if tool_registry_table_wiped:
        await ToolRegistryStore(dynamodb, settings)._seed_initial_entries()  # noqa: SLF001

    s3 = boto3.resource("s3", region_name="eu-west-2")
    for bucket in s3.buckets.all():
        bucket.objects.all().delete()

    # ECR: full delete (not just images), unlike DynamoDB above — a
    # repository has no expensive schema worth preserving the way a table
    # with a GSI does, and several tests (test_platform_version_service.py,
    # test_platform_upgrade_api.py) need the repository to be genuinely
    # ABSENT, not just empty, for their "no repository yet" cases. Both
    # files share the literal repo name "agent-builder-runtime", so this
    # also prevents cross-file image/tag pollution between them.
    ecr = boto3.client("ecr", region_name="eu-west-2")
    for repo in ecr.describe_repositories().get("repositories", []):
        ecr.delete_repository(repositoryName=repo["repositoryName"], force=True)

    secretsmanager = boto3.client("secretsmanager", region_name="eu-west-2")
    for page in secretsmanager.get_paginator("list_secrets").paginate():
        for entry in page["SecretList"]:
            if entry["Name"] not in _BASELINE_SECRET_NAMES:
                secretsmanager.delete_secret(SecretId=entry["ARN"], ForceDeleteWithoutRecovery=True)


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
