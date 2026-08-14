"""Security scan orchestration (CLAUDE.md Phase 9).

R05: the customer's CI/CD owns actual scan *execution* — CodeBuild running
Bandit/Semgrep/Trufflehog/Safety/Checkov/tfsec/Trivy (Section 6.2). This
module only starts those already-provisioned CodeBuild projects (boto3
start_build) and reports back their build ids; it never runs a scanner
itself, and it never reads their raw output. Each project is expected to
write its own SecurityScanSummary to the deployments table on completion
(see app.modules.security.policy_enforcement / codebuild/scripts).
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from typing import Any

from app.modules.security.models import SCAN_TYPES, SecurityScanType


def codebuild_project_name(scan_type: SecurityScanType) -> str:
    return f"panasa-security-{scan_type.replace('_', '-')}"


class SecurityScanner:
    def __init__(self, codebuild_client: Any) -> None:
        self._codebuild = codebuild_client

    async def start_scans(
        self,
        agent_id: str,
        deployment_id: str,
        scan_types: Sequence[SecurityScanType] = SCAN_TYPES,
    ) -> dict[str, str]:
        """Starts each requested scan project. Returns {scan_type: build_id}."""

        async def _start(scan_type: SecurityScanType) -> tuple[str, str]:
            response = await asyncio.to_thread(
                self._codebuild.start_build,
                projectName=codebuild_project_name(scan_type),
                environmentVariablesOverride=[
                    {"name": "AGENT_ID", "value": agent_id, "type": "PLAINTEXT"},
                    {"name": "DEPLOYMENT_ID", "value": deployment_id, "type": "PLAINTEXT"},
                    {"name": "SCAN_TYPE", "value": scan_type, "type": "PLAINTEXT"},
                ],
            )
            build_id: str = response["build"]["id"]
            return scan_type, build_id

        results = await asyncio.gather(*(_start(scan_type) for scan_type in scan_types))
        return dict(results)
