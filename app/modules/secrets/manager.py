"""Secrets Manager CRUD — ARN-only storage (CLAUDE.md Section 11, Phase 12).

R-rules this module exists to satisfy:
  1. Never store secret values in DynamoDB — every write here returns only
     the secret's ARN; callers (e.g. app.modules.connectors.catalog) are
     expected to persist that ARN, never the value passed in.
  2. Never log secret values — this module logs secret_name/secret_arn only,
     via structlog's usual keyword args, never the `value` parameter itself.
  3/4. Values only ever leave this module as a return value to the
     immediate caller, never to Terraform source or telemetry.
  5. Per-agent secret scoping is enforced by IAM policy (see
     app/modules/iac_generator/templates/terraform/tools/tools.tf.j2), not
     by this module — it has no concept of "whose" secret it's touching.

`get_secret_value`'s docstring note ("never cache in memory > 5 min") is
satisfied trivially and conservatively here: this module never caches at
all — every call is a fresh GetSecretValue.
"""

from __future__ import annotations

import asyncio
from typing import Any

from botocore.exceptions import ClientError

from app.shared.logging import get_logger

log = get_logger()


class SecretNotFoundError(Exception):
    def __init__(self, secret_arn: str) -> None:
        self.secret_arn = secret_arn
        super().__init__(f"Secret {secret_arn!r} not found")


class SecretsManager:
    def __init__(self, secretsmanager_client: Any) -> None:
        self._client = secretsmanager_client

    async def create_secret(self, name: str, value: str) -> str:
        """Creates a new secret and returns its ARN. Never returns/logs `value`."""
        response = await asyncio.to_thread(
            self._client.create_secret, Name=name, SecretString=value
        )
        arn: str = response["ARN"]
        log.info("secret.created", secret_name=name, secret_arn=arn)
        return arn

    async def update_secret(self, secret_arn: str, value: str) -> None:
        try:
            await asyncio.to_thread(
                self._client.put_secret_value, SecretId=secret_arn, SecretString=value
            )
        except ClientError as exc:
            if exc.response["Error"]["Code"] == "ResourceNotFoundException":
                raise SecretNotFoundError(secret_arn) from exc
            raise
        log.info("secret.updated", secret_arn=secret_arn)

    async def get_secret_value(self, secret_arn: str) -> str:
        try:
            response = await asyncio.to_thread(self._client.get_secret_value, SecretId=secret_arn)
        except ClientError as exc:
            if exc.response["Error"]["Code"] == "ResourceNotFoundException":
                raise SecretNotFoundError(secret_arn) from exc
            raise
        secret_string: str = response["SecretString"]
        return secret_string

    async def delete_secret(self, secret_arn: str, force: bool = False) -> None:
        try:
            await asyncio.to_thread(
                self._client.delete_secret,
                SecretId=secret_arn,
                ForceDeleteWithoutRecovery=force,
            )
        except ClientError as exc:
            if exc.response["Error"]["Code"] == "ResourceNotFoundException":
                raise SecretNotFoundError(secret_arn) from exc
            raise
        log.info("secret.deleted", secret_arn=secret_arn)
