"""SecurityScanner tests against moto's CodeBuild mock.

moto accepts `environmentVariablesOverride` on start_build but doesn't
persist/echo it back on the build object (confirmed empirically — the same
kind of fidelity gap as CodeCommit's missing create_commit in Phase 7).
Verifying the override was *sent* therefore uses a recording wrapper around
the real (moto-backed) client rather than reading it back from moto.
"""

from __future__ import annotations

from typing import Any

import boto3
import pytest

from app.modules.security.models import SCAN_TYPES
from app.modules.security.scanner import SecurityScanner, codebuild_project_name


class _RecordingCodeBuildClient:
    def __init__(self, real_client: Any) -> None:
        self._real = real_client
        self.start_build_calls: list[dict[str, Any]] = []

    def start_build(self, **kwargs: Any) -> Any:
        self.start_build_calls.append(kwargs)
        return self._real.start_build(**kwargs)

    def batch_get_builds(self, **kwargs: Any) -> Any:
        return self._real.batch_get_builds(**kwargs)


@pytest.fixture
def codebuild_client():
    client = boto3.client("codebuild", region_name="eu-west-2")
    for scan_type in SCAN_TYPES:
        client.create_project(
            name=codebuild_project_name(scan_type),
            source={"type": "GITHUB", "location": "https://github.com/example/repo.git"},
            artifacts={"type": "NO_ARTIFACTS"},
            environment={
                "type": "LINUX_CONTAINER",
                "image": "aws/codebuild/standard:7.0",
                "computeType": "BUILD_GENERAL1_SMALL",
            },
            serviceRole="arn:aws:iam::123456789012:role/service-role/test",
        )
    return _RecordingCodeBuildClient(client)


async def test_start_scans_starts_all_five_by_default(codebuild_client) -> None:
    scanner = SecurityScanner(codebuild_client)

    build_ids = await scanner.start_scans("agent-1", "DEP-AAAAAAAA")

    assert set(build_ids) == set(SCAN_TYPES)
    for build_id in build_ids.values():
        assert build_id  # non-empty


async def test_start_scans_accepts_a_subset(codebuild_client) -> None:
    scanner = SecurityScanner(codebuild_client)

    build_ids = await scanner.start_scans("agent-1", "DEP-AAAAAAAA", scan_types=["sast"])

    assert set(build_ids) == {"sast"}


async def test_start_scans_passes_agent_and_deployment_env_vars(codebuild_client) -> None:
    scanner = SecurityScanner(codebuild_client)

    build_ids = await scanner.start_scans("agent-42", "DEP-BBBBBBBB", scan_types=["sast"])
    assert build_ids["sast"]  # moto accepted the start_build call

    call = codebuild_client.start_build_calls[0]
    assert call["projectName"] == "panasa-security-sast"
    env_vars = {e["name"]: e["value"] for e in call["environmentVariablesOverride"]}
    assert env_vars == {
        "AGENT_ID": "agent-42",
        "DEPLOYMENT_ID": "DEP-BBBBBBBB",
        "SCAN_TYPE": "sast",
    }


def test_codebuild_project_name_convention() -> None:
    assert codebuild_project_name("sast") == "panasa-security-sast"
    assert codebuild_project_name("secret_scan") == "panasa-security-secret-scan"
