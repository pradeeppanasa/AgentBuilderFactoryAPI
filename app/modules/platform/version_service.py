"""Platform version + available-update check (CLAUDE.md Section 5.6/14
Phase 15: "GET /api/v1/platform/version — current version + available
update (checks customer-owned ECR)").

R03/R04 don't apply here the way they do to agent infrastructure — this
reads the Runtime's *own* ECR repository (settings.runtime_image, mirrored
into the customer's account at bootstrap per A13) using the Runtime's own
IAM role, the same way it already reads its own DynamoDB tables. It never
touches a customer's Terraform state or agent infrastructure.
"""

from __future__ import annotations

import asyncio
from typing import Any

from botocore.exceptions import ClientError
from packaging.version import InvalidVersion, Version
from pydantic import BaseModel

from app.config import Settings
from app.shared.logging import get_logger

log = get_logger()


class PlatformVersionInfo(BaseModel):
    platform_version: str
    runtime_version: str
    available_update: str | None = None
    update_available: bool = False


def repo_name_from_image_uri(image_uri: str) -> str | None:
    """ "{account}.dkr.ecr.{region}.amazonaws.com/{repo}:{tag}" -> "{repo}".
    Returns None for anything that doesn't look like an ECR image URI
    (e.g. a bare "myimage:latest" in local dev) — there's no repository to
    check in that case, not an error."""
    host_and_path = image_uri.split(":", 1)[0]
    if "/" not in host_and_path or ".dkr.ecr." not in host_and_path:
        return None
    return host_and_path.split("/", 1)[1]


class PlatformVersionService:
    def __init__(self, ecr_client: Any, settings: Settings) -> None:
        self._ecr = ecr_client
        self._settings = settings

    async def get_version_info(self) -> PlatformVersionInfo:
        latest = await self._highest_available_version()
        update_available = latest is not None and self._is_newer(
            latest, self._settings.platform_version
        )
        return PlatformVersionInfo(
            platform_version=self._settings.platform_version,
            runtime_version=self._settings.platform_version,
            available_update=latest if update_available else None,
            update_available=update_available,
        )

    async def _highest_available_version(self) -> str | None:
        if not self._settings.runtime_image:
            return None
        repo = repo_name_from_image_uri(self._settings.runtime_image)
        if repo is None:
            return None

        try:
            response = await asyncio.to_thread(self._ecr.describe_images, repositoryName=repo)
        except ClientError:
            log.warning("platform.version.ecr_unreachable", repository=repo, exc_info=True)
            return None

        candidates: list[Version] = []
        for image in response.get("imageDetails", []):
            for tag in image.get("imageTags", []):
                try:
                    candidates.append(Version(tag))
                except InvalidVersion:
                    continue  # e.g. "latest" — not a comparable release
        if not candidates:
            return None
        return str(max(candidates))

    def _is_newer(self, candidate: str, current: str) -> bool:
        try:
            return Version(candidate) > Version(current)
        except InvalidVersion:
            return False
