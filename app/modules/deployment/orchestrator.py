"""EventBridge deployment-request publisher (CLAUDE.md Section 6.1).

Publishing this event is the Factory Runtime's last step in triggering a
deployment. Everything downstream (Step Functions, CodeBuild, terraform
plan/apply) is a later phase and lives on the customer side (F0/F2) — the
Runtime does not process this event itself.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from typing import Any

from app.config import Settings


class DeploymentOrchestrator:
    def __init__(self, eventbridge_client: Any, settings: Settings) -> None:
        self._eventbridge = eventbridge_client
        self._settings = settings

    async def trigger_deployment(
        self, agent_id: str, version: int, deployment_id: str, tenant_id: str
    ) -> None:
        detail = {
            "deploymentId": deployment_id,
            "agentId": agent_id,
            "version": version,
            "tenantId": tenant_id,
            "triggeredAt": datetime.now(UTC).isoformat(),
        }
        await asyncio.to_thread(
            self._eventbridge.put_events,
            Entries=[
                {
                    "Source": "panasa.agent-builder",
                    "DetailType": "AgentDeploymentRequested",
                    "EventBusName": self._settings.eventbridge_bus_name,
                    "Detail": json.dumps(detail),
                }
            ],
        )

    async def notify_deployment_approved(
        self, agent_id: str, deployment_id: str, tenant_id: str, approved_by: str
    ) -> None:
        """Section 45.4 — unblock a "manual"-mode deployment parked at
        PENDING_APPROVAL. The customer's own generated CI/CD workflow
        (Section 45.6) is what's actually waiting on this event and moves
        the deployment into APPLYING; the Runtime never runs terraform
        itself (R57)."""
        detail = {
            "deploymentId": deployment_id,
            "agentId": agent_id,
            "tenantId": tenant_id,
            "approvedBy": approved_by,
            "approvedAt": datetime.now(UTC).isoformat(),
        }
        await asyncio.to_thread(
            self._eventbridge.put_events,
            Entries=[
                {
                    "Source": "panasa.agent-builder",
                    "DetailType": "AgentDeploymentApproved",
                    "EventBusName": self._settings.eventbridge_bus_name,
                    "Detail": json.dumps(detail),
                }
            ],
        )
