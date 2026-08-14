"""Unit tests for app.modules.secrets.manager (CLAUDE.md Section 11).

No test for delete_secret() against an unknown ARN raising SecretNotFoundError
— moto's delete_secret silently no-ops for an unknown SecretId instead of
raising ResourceNotFoundException like real AWS does (confirmed empirically;
the same kind of fidelity gap as test_security_scanner.py's CodeCommit note).
The real-AWS-facing behavior (raise on the ClientError code) is still
implemented in manager.py — it's only moto's simulation that can't exercise it.
"""

from __future__ import annotations

import boto3
import pytest

from app.modules.secrets.manager import SecretNotFoundError, SecretsManager


@pytest.fixture
def secrets_manager() -> SecretsManager:
    client = boto3.client("secretsmanager", region_name="eu-west-2")
    return SecretsManager(client)


async def test_create_and_get_secret_round_trips(secrets_manager: SecretsManager) -> None:
    arn = await secrets_manager.create_secret("tool/jira/api-key", "super-secret-value")
    assert "tool/jira/api-key" in arn

    value = await secrets_manager.get_secret_value(arn)
    assert value == "super-secret-value"


async def test_update_secret_changes_value(secrets_manager: SecretsManager) -> None:
    arn = await secrets_manager.create_secret("tool/salesforce/token", "v1")
    await secrets_manager.update_secret(arn, "v2")

    assert await secrets_manager.get_secret_value(arn) == "v2"


async def test_get_secret_value_unknown_arn_raises(secrets_manager: SecretsManager) -> None:
    with pytest.raises(SecretNotFoundError):
        await secrets_manager.get_secret_value("arn:aws:secretsmanager:eu-west-2:123:secret:nope")


async def test_update_secret_unknown_arn_raises(secrets_manager: SecretsManager) -> None:
    with pytest.raises(SecretNotFoundError):
        await secrets_manager.update_secret("arn:aws:secretsmanager:eu-west-2:123:secret:nope", "x")


async def test_delete_secret_removes_it(secrets_manager: SecretsManager) -> None:
    arn = await secrets_manager.create_secret("tool/temp/key", "value")
    await secrets_manager.delete_secret(arn, force=True)

    with pytest.raises(SecretNotFoundError):
        await secrets_manager.get_secret_value(arn)
