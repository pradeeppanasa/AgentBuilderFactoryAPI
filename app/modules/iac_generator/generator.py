"""IaC Generator orchestrator (CLAUDE.md Section 8 / Phase 6).

Resolves required modules (conditional.py, R20), renders them via the
active IaCBackend (IAC_TOOL setting — terraform | cdk, pluggable the same
way GitProvider is), zips the result, and uploads it to the IaC artifacts
bucket.

R03/F0/F2: this produces desired-state IaC *source* only. It never touches
Terraform/CDK state and never calls AWS to apply anything — that's the
customer-side CI/CD's job, in a later phase.
"""

from __future__ import annotations

import asyncio
import io
import zipfile
from dataclasses import dataclass
from typing import Any

from app.config import Settings
from app.modules.iac_generator.backends.base import IaCBackend
from app.modules.iac_generator.backends.cdk import CDKBackend
from app.modules.iac_generator.backends.terraform import TerraformBackend
from app.modules.iac_generator.conditional import resolve_required_modules
from app.modules.registry.models import AgentConfiguration


@dataclass(frozen=True)
class IaCGenerationResult:
    tool: str
    iac_version: str
    s3_key: str
    modules: list[str]
    files: dict[str, str]
    """Raw rendered {path: content} — e.g. for the deploy flow's git commit.
    Not part of the generate-iac API response; the route builds its response
    from the other fields only."""


class IaCGenerator:
    def __init__(self, s3_client: Any, settings: Settings) -> None:
        self._s3 = s3_client
        self._settings = settings
        self._backends: dict[str, IaCBackend] = {
            "terraform": TerraformBackend(),
            "cdk": CDKBackend(),
        }

    async def generate(
        self, agent_id: str, tenant_id: str, version: int, config: AgentConfiguration
    ) -> IaCGenerationResult:
        if not self._settings.iac_output_bucket:
            raise RuntimeError("IAC_OUTPUT_BUCKET is not configured")

        backend = self._backends[self._settings.iac_tool]
        resolved_modules = resolve_required_modules(config)

        files = backend.render(
            agent_id, tenant_id, version, config, resolved_modules, self._settings
        )
        archive = _build_zip(files)

        iac_version = f"1.0.{version}"
        s3_key = (
            f"iac/{backend.tool_name}/{agent_id}/v{version}/"
            f"{agent_id}-v{version}-{iac_version}.zip"
        )

        await asyncio.to_thread(
            self._s3.put_object,
            Bucket=self._settings.iac_output_bucket,
            Key=s3_key,
            Body=archive,
            ContentType="application/zip",
        )

        return IaCGenerationResult(
            tool=backend.tool_name,
            iac_version=iac_version,
            s3_key=s3_key,
            modules=resolved_modules,
            files=files,
        )


def _build_zip(files: dict[str, str]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, mode="w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path, content in sorted(files.items()):
            archive.writestr(path, content)
    return buffer.getvalue()
