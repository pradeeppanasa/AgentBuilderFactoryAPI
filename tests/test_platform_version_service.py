"""Unit tests for app.modules.platform.version_service (Phase 15).

ECR access goes through moto's mocked backend (conftest.py's autouse
mocked_aws fixture already has us inside a `mock_aws()` context; each test
here creates its own repository/images on top of that).
"""

from __future__ import annotations

import json

import boto3
import pytest

from app.config import settings
from app.modules.platform.version_service import (
    PlatformVersionService,
    repo_name_from_image_uri,
)

_REPO = "agent-builder-runtime"
_ECR_URI = f"123456789012.dkr.ecr.eu-west-2.amazonaws.com/{_REPO}"
_MANIFEST = json.dumps(
    {
        "schemaVersion": 2,
        "mediaType": "application/vnd.docker.distribution.manifest.v2+json",
        "config": {"mediaType": "x", "size": 1, "digest": f"sha256:{'0' * 64}"},
        "layers": [],
    }
)


def _push_tag(ecr_client, tag: str) -> None:
    ecr_client.put_image(repositoryName=_REPO, imageManifest=_MANIFEST, imageTag=tag)


@pytest.mark.parametrize(
    ("image_uri", "expected"),
    [
        (f"{_ECR_URI}:1.2.0", _REPO),
        (f"{_ECR_URI}:latest", _REPO),
        ("myimage:latest", None),
        ("myimage", None),
        ("123456789012.dkr.ecr.eu-west-2.amazonaws.com/nested/repo:1.0.0", "nested/repo"),
    ],
)
def test_repo_name_from_image_uri(image_uri: str, expected: str | None) -> None:
    assert repo_name_from_image_uri(image_uri) == expected


async def test_get_version_info_no_runtime_image_configured() -> None:
    stub_settings = settings.model_copy(update={"runtime_image": None})
    service = PlatformVersionService(boto3.client("ecr", region_name="eu-west-2"), stub_settings)

    info = await service.get_version_info()

    assert info.platform_version == stub_settings.platform_version
    assert info.update_available is False
    assert info.available_update is None


async def test_get_version_info_local_image_uri_not_checked() -> None:
    stub_settings = settings.model_copy(update={"runtime_image": "agent-builder-runtime:latest"})
    service = PlatformVersionService(boto3.client("ecr", region_name="eu-west-2"), stub_settings)

    info = await service.get_version_info()

    assert info.update_available is False
    assert info.available_update is None


async def test_get_version_info_repository_missing_returns_no_update() -> None:
    stub_settings = settings.model_copy(update={"runtime_image": f"{_ECR_URI}:1.0.0"})
    service = PlatformVersionService(boto3.client("ecr", region_name="eu-west-2"), stub_settings)

    info = await service.get_version_info()

    assert info.update_available is False
    assert info.available_update is None


async def test_get_version_info_repository_empty_returns_no_update() -> None:
    ecr = boto3.client("ecr", region_name="eu-west-2")
    ecr.create_repository(repositoryName=_REPO)
    stub_settings = settings.model_copy(update={"runtime_image": f"{_ECR_URI}:1.0.0"})
    service = PlatformVersionService(ecr, stub_settings)

    info = await service.get_version_info()

    assert info.update_available is False
    assert info.available_update is None


async def test_get_version_info_finds_higher_semver_tag() -> None:
    ecr = boto3.client("ecr", region_name="eu-west-2")
    ecr.create_repository(repositoryName=_REPO)
    _push_tag(ecr, "1.0.0")
    _push_tag(ecr, "1.2.0")
    _push_tag(ecr, "1.1.0")
    _push_tag(ecr, "latest")  # not a comparable release — must be skipped, not crash

    stub_settings = settings.model_copy(update={"runtime_image": f"{_ECR_URI}:1.0.0"})
    service = PlatformVersionService(ecr, stub_settings)

    info = await service.get_version_info()

    assert info.platform_version == stub_settings.platform_version
    assert info.update_available is True
    assert info.available_update == "1.2.0"


async def test_get_version_info_current_is_already_highest() -> None:
    ecr = boto3.client("ecr", region_name="eu-west-2")
    ecr.create_repository(repositoryName=_REPO)
    _push_tag(ecr, "1.0.0")

    stub_settings = settings.model_copy(
        update={"runtime_image": f"{_ECR_URI}:1.0.0", "platform_version": "1.0.0"}
    )
    service = PlatformVersionService(ecr, stub_settings)

    info = await service.get_version_info()

    assert info.update_available is False
    assert info.available_update is None


async def test_get_version_info_only_non_semver_tags_returns_no_update() -> None:
    ecr = boto3.client("ecr", region_name="eu-west-2")
    ecr.create_repository(repositoryName=_REPO)
    _push_tag(ecr, "latest")
    _push_tag(ecr, "dev")

    stub_settings = settings.model_copy(update={"runtime_image": f"{_ECR_URI}:1.0.0"})
    service = PlatformVersionService(ecr, stub_settings)

    info = await service.get_version_info()

    assert info.update_available is False
    assert info.available_update is None
