"""Secrets Manager client factory. Same local-dev-endpoint-override pattern
as dynamodb.py/s3.py."""

from __future__ import annotations

from typing import Any

import boto3

from app.config import Settings


def create_secrets_manager_client(settings: Settings) -> Any:
    kwargs: dict[str, Any] = {"region_name": settings.aws_region}
    if settings.secrets_manager_endpoint:
        kwargs["endpoint_url"] = settings.secrets_manager_endpoint
    return boto3.client("secretsmanager", **kwargs)
