"""Fetches the git host credential from Secrets Manager at startup.

Section 10: "Git credentials fetched from Secrets Manager at runtime.
Never stored in config. Never logged." CodeCommit is the one exception —
it authenticates with the Runtime's own AWS credentials (SigV4), not a
personal access token, so there is nothing to fetch for that provider.
"""

from __future__ import annotations

import boto3

from app.config import Settings


def fetch_git_token(settings: Settings) -> str | None:
    if settings.git_provider == "codecommit":
        return None

    if not settings.git_credentials_secret:
        raise RuntimeError("GIT_CREDENTIALS_SECRET is not configured")

    kwargs: dict[str, str] = {"region_name": settings.aws_region}
    if settings.secrets_manager_endpoint:
        kwargs["endpoint_url"] = settings.secrets_manager_endpoint

    client = boto3.client("secretsmanager", **kwargs)
    response = client.get_secret_value(SecretId=settings.git_credentials_secret)
    secret_string: str = response["SecretString"]
    return secret_string
