"""GET /platform/version, POST /platform/upgrade, GET /platform/upgrades/{id}
(Phase 15) — end-to-end through the real app. ECR and Step Functions are
real moto-backed clients (constructed in app.main's lifespan); settings
attributes that gate these routes (runtime_image,
platform_upgrade_state_machine_arn) are mutated in place via monkeypatch
since app.api.v1.platform and the Phase 15 services all hold a reference to
the same `app.config.settings` singleton.
"""

from __future__ import annotations

import contextlib
import json

import boto3
import pytest
from fastapi.testclient import TestClient

from app.config import settings
from app.main import app

TENANT_A = "tenant-a"
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


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _push_tag(tag: str) -> None:
    ecr = boto3.client("ecr", region_name="eu-west-2")
    with contextlib.suppress(ecr.exceptions.RepositoryAlreadyExistsException):
        ecr.create_repository(repositoryName=_REPO)
    ecr.put_image(repositoryName=_REPO, imageManifest=_MANIFEST, imageTag=tag)


def _create_state_machine() -> str:
    sfn = boto3.client("stepfunctions", region_name="eu-west-2")
    definition = json.dumps({"StartAt": "A", "States": {"A": {"Type": "Pass", "End": True}}})
    response = sfn.create_state_machine(
        name="test-platform-upgrade-api",
        definition=definition,
        roleArn="arn:aws:iam::123456789012:role/fake-deployment-role",
    )
    return str(response["stateMachineArn"])


def test_get_version_requires_no_auth_and_reports_no_update_by_default() -> None:
    with TestClient(app) as client:
        response = client.get("/api/v1/platform/version")

    assert response.status_code == 200
    body = response.json()
    assert body["platform_version"] == settings.platform_version
    assert body["update_available"] is False
    assert body["available_update"] is None


def test_get_version_reports_available_update(monkeypatch: pytest.MonkeyPatch) -> None:
    _push_tag("1.0.0")
    _push_tag("9.9.9")
    monkeypatch.setattr(settings, "runtime_image", f"{_ECR_URI}:1.0.0")

    with TestClient(app) as client:
        response = client.get("/api/v1/platform/version")

    assert response.status_code == 200
    body = response.json()
    assert body["update_available"] is True
    assert body["available_update"] == "9.9.9"


async def test_upgrade_forbidden_for_non_admin(make_user_and_token) -> None:
    _, developer_token = await make_user_and_token(TENANT_A, role="developer")

    with TestClient(app) as client:
        response = client.post(
            "/api/v1/platform/upgrade", json={}, headers=_bearer(developer_token)
        )

    assert response.status_code == 403


async def test_upgrade_400_when_no_target_and_no_update_available(make_user_and_token) -> None:
    _, admin_token = await make_user_and_token(TENANT_A, role="admin")

    with TestClient(app) as client:
        response = client.post("/api/v1/platform/upgrade", json={}, headers=_bearer(admin_token))

    assert response.status_code == 400


async def test_upgrade_500_when_runtime_image_not_configured(
    make_user_and_token, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "runtime_image", None)
    _, admin_token = await make_user_and_token(TENANT_A, role="admin")

    with TestClient(app) as client:
        response = client.post(
            "/api/v1/platform/upgrade",
            json={"target_version": "1.1.0"},
            headers=_bearer(admin_token),
        )

    assert response.status_code == 500


async def test_upgrade_500_when_state_machine_not_configured(
    make_user_and_token, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "runtime_image", f"{_ECR_URI}:1.0.0")
    monkeypatch.setattr(settings, "platform_upgrade_state_machine_arn", None)
    _, admin_token = await make_user_and_token(TENANT_A, role="admin")

    with TestClient(app) as client:
        response = client.post(
            "/api/v1/platform/upgrade",
            json={"target_version": "1.1.0"},
            headers=_bearer(admin_token),
        )

    assert response.status_code == 500


async def test_upgrade_happy_path_explicit_target_version(
    make_user_and_token, monkeypatch: pytest.MonkeyPatch
) -> None:
    state_machine_arn = _create_state_machine()
    monkeypatch.setattr(settings, "runtime_image", f"{_ECR_URI}:1.0.0")
    monkeypatch.setattr(settings, "platform_upgrade_state_machine_arn", state_machine_arn)
    _, admin_token = await make_user_and_token(TENANT_A, role="admin")

    with TestClient(app) as client:
        response = client.post(
            "/api/v1/platform/upgrade",
            json={"target_version": "1.1.0"},
            headers=_bearer(admin_token),
        )

        assert response.status_code == 202
        body = response.json()
        assert body["upgrade_id"].startswith("UPG-")
        assert body["status"] == "PENDING"
        assert body["from_version"] == settings.platform_version
        assert body["target_version"] == "1.1.0"
        assert body["execution_arn"]

        detail = client.get(
            f"/api/v1/platform/upgrades/{body['upgrade_id']}", headers=_bearer(admin_token)
        )

    assert detail.status_code == 200
    detail_body = detail.json()
    assert detail_body["target_version"] == "1.1.0"
    assert detail_body["target_image"] == f"{_ECR_URI}:1.1.0"
    assert set(detail_body["stages"].keys()) == {
        "PULLING_IMAGE",
        "REGISTERING_TASK_DEFINITION",
        "UPDATING_SERVICE",
        "HEALTH_CHECK",
    }


async def test_upgrade_happy_path_defaults_to_highest_available(
    make_user_and_token, monkeypatch: pytest.MonkeyPatch
) -> None:
    state_machine_arn = _create_state_machine()
    _push_tag("1.0.0")
    _push_tag("2.5.0")
    monkeypatch.setattr(settings, "runtime_image", f"{_ECR_URI}:1.0.0")
    monkeypatch.setattr(settings, "platform_upgrade_state_machine_arn", state_machine_arn)
    _, admin_token = await make_user_and_token(TENANT_A, role="admin")

    with TestClient(app) as client:
        response = client.post("/api/v1/platform/upgrade", json={}, headers=_bearer(admin_token))

    assert response.status_code == 202
    assert response.json()["target_version"] == "2.5.0"


async def test_get_upgrade_404_for_unknown_id(make_user_and_token) -> None:
    _, admin_token = await make_user_and_token(TENANT_A, role="admin")

    with TestClient(app) as client:
        response = client.get(
            "/api/v1/platform/upgrades/UPG-DOESNOTEXIST", headers=_bearer(admin_token)
        )

    assert response.status_code == 404


async def test_get_upgrade_forbidden_for_non_admin(make_user_and_token) -> None:
    _, developer_token = await make_user_and_token(TENANT_A, role="developer")

    with TestClient(app) as client:
        response = client.get(
            "/api/v1/platform/upgrades/UPG-WHATEVER", headers=_bearer(developer_token)
        )

    assert response.status_code == 403
