"""DynamoDB resource factory.

Local dev / test points at DynamoDB Local (or a moto mock) via
`settings.dynamodb_endpoint`. In deployed environments (prototype or
enterprise) this is unset and boto3 talks to real DynamoDB in the
account the Runtime is running in.

The resource is created once at app startup (see app/main.py lifespan) and
handed to modules via app.state — not module-level caching — so tests can
spin up a fresh, isolated resource per moto mock context.
"""

from __future__ import annotations

from typing import Any

import boto3

from app.config import Settings


def create_dynamodb_resource(settings: Settings) -> Any:
    kwargs: dict[str, Any] = {"region_name": settings.aws_region}
    if settings.dynamodb_endpoint:
        kwargs["endpoint_url"] = settings.dynamodb_endpoint
    return boto3.resource("dynamodb", **kwargs)
