"""Platform upgrade trigger (CLAUDE.md Section 14 Phase 15: "POST
/api/v1/platform/upgrade — triggers Step Functions upgrade workflow").

Unlike app.modules.deployment.orchestrator (which publishes an EventBridge
event the customer's own Step Functions picks up — F0/R05, since agent
infrastructure changes are the customer CI/CD's job), this calls
stepfunctions:StartExecution directly. There is no customer-CI/CD
indirection to preserve here: upgrading the Factory's own ECS service is
this Runtime's own operational concern, not agent infrastructure, so R05's
boundary doesn't apply — see version_service.py's docstring for the same
point about the ECR check.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from app.config import Settings


class PlatformUpgradeNotConfiguredError(Exception):
    pass


class PlatformUpgradeOrchestrator:
    def __init__(self, stepfunctions_client: Any, settings: Settings) -> None:
        self._sfn = stepfunctions_client
        self._settings = settings

    async def start_upgrade(
        self,
        upgrade_id: str,
        from_version: str,
        target_version: str,
        target_image: str,
    ) -> str:
        if not self._settings.platform_upgrade_state_machine_arn:
            raise PlatformUpgradeNotConfiguredError(
                "PLATFORM_UPGRADE_STATE_MACHINE_ARN is not configured"
            )

        response = await asyncio.to_thread(
            self._sfn.start_execution,
            stateMachineArn=self._settings.platform_upgrade_state_machine_arn,
            name=upgrade_id,
            input=json.dumps(
                {
                    "upgradeId": upgrade_id,
                    "fromVersion": from_version,
                    "targetVersion": target_version,
                    "targetImage": target_image,
                }
            ),
        )
        execution_arn: str = response["executionArn"]
        return execution_arn
