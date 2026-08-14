"""Version snapshotting + persistence for panasa-agent-versions (Phase 4).

R08: every version is a brand-new DynamoDB item, never a mutation of an
existing one. The write uses a condition expression so an accidental
overwrite of an existing version fails loudly instead of silently
corrupting history — belt-and-suspenders on top of the store always
computing `current_version + 1` for the next version number.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from typing import Any

from boto3.dynamodb.conditions import Attr, Key
from botocore.exceptions import ClientError

from app.modules.registry.contract_generator import CapabilityContractGenerator
from app.modules.registry.models import AgentConfiguration, AgentType, AgentVersionRecord
from app.shared.dynamodb_types import decimal_to_native

_JSON_FIELDS = {"configuration", "capability_contract", "security_result", "evaluation_result"}

# Fields explicitly documented (Section 4.2) as "populated after pipeline
# runs" — the one exception to R08's "never update an existing version
# record", which protects the immutable configuration/capability_contract
# snapshot, not these derived artifacts. security_result isn't in this
# allowlist yet: it's a JSON-blob field with no writer until the Security
# Scanning phase's own version-record wiring exists (Phase 9 only wrote scan
# results to the deployment stages, not the version record — see
# app.modules.security.policy_enforcement). evaluation_result was added in
# Phase 10 (app.modules.registry.store.AgentRegistryStore.
# record_evaluation_result).
_MUTABLE_DERIVED_FIELDS = {
    "iac_version",
    "iac_s3_key",
    "deployment_id",
    "terraform_plan_summary",
    "deployment_result",
    "evaluation_result",
}


def _now() -> str:
    return datetime.now(UTC).isoformat()


class VersionAlreadyExistsError(Exception):
    def __init__(self, agent_id: str, version: int) -> None:
        self.agent_id = agent_id
        self.version = version
        super().__init__(f"Version {version} already exists for agent {agent_id!r}")


class AgentVersioner:
    def __init__(
        self, versions_table: Any, contract_generator: CapabilityContractGenerator
    ) -> None:
        self._versions_table = versions_table
        self._contract_generator = contract_generator

    def build_version(
        self,
        agent_id: str,
        agent_name: str,
        agent_type: AgentType,
        version: int,
        configuration: AgentConfiguration,
        changed_by: str,
        change_description: str,
        rolled_back_from_version: int | None = None,
    ) -> AgentVersionRecord:
        contract = self._contract_generator.generate(
            agent_id=agent_id,
            agent_name=agent_name,
            agent_type=agent_type,
            config=configuration,
            version=version,
        )
        return AgentVersionRecord(
            agent_id=agent_id,
            version=version,
            version_status="DRAFT",
            change_description=change_description,
            changed_by=changed_by,
            created_at=_now(),
            configuration=configuration,
            capability_contract=contract,
            rolled_back_from_version=rolled_back_from_version,
        )

    async def write(self, version_record: AgentVersionRecord) -> None:
        try:
            await asyncio.to_thread(
                self._versions_table.put_item,
                Item=self._to_item(version_record),
                ConditionExpression=Attr("version").not_exists(),
            )
        except ClientError as exc:
            if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
                raise VersionAlreadyExistsError(
                    version_record.agent_id, version_record.version
                ) from exc
            raise

    async def record_derived_fields(self, agent_id: str, version: int, **fields: Any) -> None:
        """Update only pipeline-derived fields on an existing version item.

        Raises ValueError if asked to touch anything outside
        _MUTABLE_DERIVED_FIELDS — the immutable configuration/
        capability_contract snapshot is never reachable through this path.
        """
        unknown = set(fields) - _MUTABLE_DERIVED_FIELDS
        if unknown:
            raise ValueError(f"Cannot update non-derived version fields: {sorted(unknown)}")
        if not fields:
            return

        # JSON-blob fields (e.g. evaluation_result) must be stored as a JSON
        # string here too, matching _to_item/_from_item — otherwise boto3's
        # Table resource would write them as a native DynamoDB Map and
        # _from_item's json.loads() on read would fail against a dict.
        values = {
            f":{name}": (json.dumps(value) if name in _JSON_FIELDS and value is not None else value)
            for name, value in fields.items()
        }

        update_expression = "SET " + ", ".join(f"#{name} = :{name}" for name in fields)
        await asyncio.to_thread(
            self._versions_table.update_item,
            Key={"agent_id": agent_id, "version": version},
            UpdateExpression=update_expression,
            ExpressionAttributeNames={f"#{name}": name for name in fields},
            ExpressionAttributeValues=values,
        )

    async def get(self, agent_id: str, version: int) -> AgentVersionRecord | None:
        response = await asyncio.to_thread(
            self._versions_table.get_item, Key={"agent_id": agent_id, "version": version}
        )
        item = response.get("Item")
        if item is None:
            return None
        return self._from_item(item)

    async def list_all(self, agent_id: str) -> list[AgentVersionRecord]:
        """Full version history, newest first."""
        versions: list[AgentVersionRecord] = []
        exclusive_start_key: dict[str, Any] | None = None
        while True:
            query_kwargs: dict[str, Any] = {
                "KeyConditionExpression": Key("agent_id").eq(agent_id),
                "ScanIndexForward": False,
            }
            if exclusive_start_key is not None:
                query_kwargs["ExclusiveStartKey"] = exclusive_start_key
            response = await asyncio.to_thread(self._versions_table.query, **query_kwargs)
            versions.extend(self._from_item(item) for item in response.get("Items", []))
            exclusive_start_key = response.get("LastEvaluatedKey")
            if exclusive_start_key is None:
                break
        return versions

    @staticmethod
    def _to_item(version_record: AgentVersionRecord) -> dict[str, Any]:
        data = version_record.model_dump(mode="json")
        for field in _JSON_FIELDS:
            if data.get(field) is not None:
                data[field] = json.dumps(data[field])
        return data

    @staticmethod
    def _from_item(item: dict[str, Any]) -> AgentVersionRecord:
        data = decimal_to_native(dict(item))
        for field in _JSON_FIELDS:
            if data.get(field):
                data[field] = json.loads(data[field])
        return AgentVersionRecord(**data)
