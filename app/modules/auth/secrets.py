"""Fetches the JWT signing secret from Secrets Manager at startup.

Rule 2 (CLAUDE.md Section 11): never store secret values in DynamoDB or env
vars directly — only ARN references, resolved at runtime. Unlike the license
token (enterprise-only, F11), the JWT secret is required in every deployment
mode, so there is no skip path here: a missing/unreachable secret fails
startup rather than silently disabling auth.
"""

from __future__ import annotations

import boto3

from app.config import Settings


def fetch_jwt_secret(settings: Settings) -> str:
    if not settings.jwt_secret_arn:
        raise RuntimeError("JWT_SECRET_ARN is not configured")

    kwargs: dict[str, str] = {"region_name": settings.aws_region}
    if settings.secrets_manager_endpoint:
        kwargs["endpoint_url"] = settings.secrets_manager_endpoint

    client = boto3.client("secretsmanager", **kwargs)
    response = client.get_secret_value(SecretId=settings.jwt_secret_arn)
    secret_string: str = response["SecretString"]
    return secret_string
