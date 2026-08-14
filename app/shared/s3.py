"""S3 client factory. Same local-dev-endpoint-override pattern as dynamodb.py."""

from __future__ import annotations

from typing import Any

import boto3

from app.config import Settings


def create_s3_client(settings: Settings) -> Any:
    kwargs: dict[str, Any] = {"region_name": settings.aws_region}
    if settings.s3_endpoint:
        kwargs["endpoint_url"] = settings.s3_endpoint
    return boto3.client("s3", **kwargs)
