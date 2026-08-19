"""Agent Registry — DynamoDB CRUD (Section 4.1/4.2, Section 5.1/5.2, R01/R02/R08/R11/R12).

R01: tenant_id is required on every operation here. No exceptions — the
versions table has no tenant_id column, so every version-scoped read/write
routes through _require_agent()/get_agent() first to enforce ownership.
R08: every version is a brand-new panasa-agent-versions item, never a
mutation of an existing one (see modules/registry/versioner.py).
R11: every version gets an auto-generated AgentCapabilityContract.
R12: orchestrator sub_agents are checked for circular dependencies on save.
"""

from __future__ import annotations

import asyncio
import base64
import json
import re
import uuid
from datetime import UTC, datetime
from typing import Any

from boto3.dynamodb.conditions import Attr, Key
from botocore.exceptions import ClientError

from app.config import Settings
from app.modules.iac_generator.validation_models import IaCValidationReport
from app.modules.registry.contract_generator import CapabilityContractGenerator
from app.modules.registry.dependency_validator import CircularDependencyValidator
from app.modules.registry.models import (
    AgentConfiguration,
    AgentRecord,
    AgentStatus,
    AgentType,
    AgentVersionRecord,
    EvaluationResult,
    ProjectLifecycleStatus,
    normalise_agent_type,
)
from app.modules.registry.versioner import AgentVersioner
from app.shared.dynamodb_types import decimal_to_native
from app.shared.exceptions import (
    AgentNotFoundError,
    CircularDependencyError,
    InvalidRollbackError,
    VersionNotFoundError,
)


def _slugify(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return slug or "agent"


def _new_agent_id(name: str) -> str:
    return f"{_slugify(name)}-{uuid.uuid4().hex[:6]}"


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _normalise_agent_item(item: dict[str, Any]) -> dict[str, Any]:
    """Map a retired agent_type value (Section 38.6) to the current
    vocabulary before an AgentRecord is constructed from a raw DynamoDB
    item — old records must stay readable with no migration required."""
    if "agent_type" in item:
        item = {**item, "agent_type": normalise_agent_type(item["agent_type"])}
    return item


def _encode_cursor(key: dict[str, Any]) -> str:
    return base64.urlsafe_b64encode(json.dumps(key).encode()).decode()


def _decode_cursor(cursor: str) -> dict[str, Any]:
    decoded: dict[str, Any] = json.loads(base64.urlsafe_b64decode(cursor.encode()).decode())
    return decoded


_PROJECT_INDEX = "project-index"


def _agent_item(record: AgentRecord) -> dict[str, Any]:
    """AgentRecord -> DynamoDB item. `project_id` is the project-index GSI's
    hash key — DynamoDB rejects a PutItem where a GSI key attribute is
    present with a NULL type, so it must be OMITTED (not written as
    null/None) for the many agents that have no project_id (every flat
    /api/v1/agents agent, Section 5.1 — project scoping is Section 38-only)."""
    item = record.model_dump(mode="json")
    if item.get("project_id") is None:
        item.pop("project_id", None)
    return item


class AgentRegistryStore:
    def __init__(self, dynamodb_resource: Any, settings: Settings) -> None:
        self._dynamodb = dynamodb_resource
        self._settings = settings
        self._agents_table = dynamodb_resource.Table(settings.dynamodb_agents_table)
        self._versions_table = dynamodb_resource.Table(settings.dynamodb_versions_table)
        self._contract_generator = CapabilityContractGenerator()
        self._dependency_validator = CircularDependencyValidator()
        self._versioner = AgentVersioner(self._versions_table, self._contract_generator)

    # ── Table lifecycle ──────────────────────────────────────────────────

    async def ensure_tables(self) -> None:
        await asyncio.gather(
            self._ensure_table(
                self._settings.dynamodb_agents_table,
                key_schema=[
                    {"AttributeName": "tenant_id", "KeyType": "HASH"},
                    {"AttributeName": "agent_id", "KeyType": "RANGE"},
                ],
                attribute_definitions=[
                    {"AttributeName": "tenant_id", "AttributeType": "S"},
                    {"AttributeName": "agent_id", "AttributeType": "S"},
                    {"AttributeName": "project_id", "AttributeType": "S"},
                ],
                global_secondary_indexes=[
                    {
                        "IndexName": _PROJECT_INDEX,
                        "KeySchema": [{"AttributeName": "project_id", "KeyType": "HASH"}],
                        "Projection": {"ProjectionType": "ALL"},
                    }
                ],
            ),
            self._ensure_table(
                self._settings.dynamodb_versions_table,
                key_schema=[
                    {"AttributeName": "agent_id", "KeyType": "HASH"},
                    {"AttributeName": "version", "KeyType": "RANGE"},
                ],
                attribute_definitions=[
                    {"AttributeName": "agent_id", "AttributeType": "S"},
                    {"AttributeName": "version", "AttributeType": "N"},
                ],
            ),
        )

    async def _ensure_table(
        self,
        table_name: str,
        key_schema: list[dict[str, str]],
        attribute_definitions: list[dict[str, str]],
        global_secondary_indexes: list[dict[str, Any]] | None = None,
    ) -> None:
        def _create() -> None:
            try:
                kwargs: dict[str, Any] = {
                    "TableName": table_name,
                    "KeySchema": key_schema,
                    "AttributeDefinitions": attribute_definitions,
                    "BillingMode": "PAY_PER_REQUEST",
                }
                if global_secondary_indexes:
                    kwargs["GlobalSecondaryIndexes"] = global_secondary_indexes
                table = self._dynamodb.create_table(**kwargs)
                table.wait_until_exists()
            except ClientError as exc:
                if exc.response["Error"]["Code"] != "ResourceInUseException":
                    raise

        await asyncio.to_thread(_create)

    # ── Create ───────────────────────────────────────────────────────────

    async def create_agent(
        self,
        tenant_id: str,
        name: str,
        description: str,
        business_purpose: str,
        agent_type: AgentType,
        configuration: AgentConfiguration,
        created_by: str,
        project_id: str | None = None,
        owner_email: str | None = None,
        tags: dict[str, str] | None = None,
        changelog: str | None = None,
    ) -> tuple[AgentRecord, AgentVersionRecord]:
        await self._validate_no_circular_dependency(
            tenant_id, agent_id=None, configuration=configuration
        )

        agent_id = await self._allocate_agent_id(name)
        now = _now()

        record = AgentRecord(
            tenant_id=tenant_id,
            agent_id=agent_id,
            name=name,
            description=description,
            business_purpose=business_purpose,
            agent_type=agent_type,
            current_version=1,
            live_version=None,
            status="DRAFT",
            platform_version=self._settings.platform_version,
            runtime_version=self._settings.platform_version,
            created_by=created_by,
            created_at=now,
            updated_by=created_by,
            updated_at=now,
            tags=tags or {},
            project_id=project_id,
            project_lifecycle_status="draft" if project_id is not None else None,
            owner_email=owner_email,
        )
        version_record = self._versioner.build_version(
            agent_id=agent_id,
            agent_name=name,
            agent_type=agent_type,
            version=1,
            configuration=configuration,
            changed_by=created_by,
            change_description=changelog or "Initial version",
        )

        await asyncio.to_thread(self._agents_table.put_item, Item=_agent_item(record))
        await self._versioner.write(version_record)
        return record, version_record

    async def _allocate_agent_id(self, name: str, attempts: int = 5) -> str:
        for _ in range(attempts):
            candidate = _new_agent_id(name)
            existing = await asyncio.to_thread(
                self._agents_table.scan,
                FilterExpression=Attr("agent_id").eq(candidate),
                Limit=1,
            )
            if not existing.get("Items"):
                return candidate
        raise RuntimeError("Failed to allocate a unique agent_id after several attempts")

    # ── Read ─────────────────────────────────────────────────────────────

    async def get_agent(self, tenant_id: str, agent_id: str) -> AgentRecord | None:
        response = await asyncio.to_thread(
            self._agents_table.get_item, Key={"tenant_id": tenant_id, "agent_id": agent_id}
        )
        item = response.get("Item")
        if item is None:
            return None
        return AgentRecord(**_normalise_agent_item(decimal_to_native(item)))

    async def _require_agent(self, tenant_id: str, agent_id: str) -> AgentRecord:
        record = await self.get_agent(tenant_id, agent_id)
        if record is None:
            raise AgentNotFoundError(agent_id)
        return record

    async def get_version(self, agent_id: str, version: int) -> AgentVersionRecord | None:
        """Internal lookup — no tenant check. Only call after ownership is
        already established elsewhere in the same request (e.g. via
        get_agent/_require_agent). Use get_version_detail from API routes."""
        return await self._versioner.get(agent_id, version)

    async def get_version_detail(
        self, tenant_id: str, agent_id: str, version: int
    ) -> AgentVersionRecord | None:
        await self._require_agent(tenant_id, agent_id)
        return await self._versioner.get(agent_id, version)

    async def list_versions(self, tenant_id: str, agent_id: str) -> list[AgentVersionRecord]:
        """Full version history for an agent, newest first."""
        await self._require_agent(tenant_id, agent_id)
        return await self._versioner.list_all(agent_id)

    async def record_iac_artifact(
        self,
        tenant_id: str,
        agent_id: str,
        version: int,
        iac_version: str,
        iac_s3_key: str,
        iac_modules: list[str] | None = None,
        iac_validation_report: IaCValidationReport | None = None,
    ) -> None:
        await self._require_agent(tenant_id, agent_id)
        if await self._versioner.get(agent_id, version) is None:
            raise VersionNotFoundError(agent_id, version)
        fields: dict[str, Any] = {"iac_version": iac_version, "iac_s3_key": iac_s3_key}
        if iac_modules is not None:
            fields["iac_modules"] = iac_modules
        if iac_validation_report is not None:
            fields["iac_validation_report"] = iac_validation_report.model_dump(mode="json")
        await self._versioner.record_derived_fields(agent_id, version, **fields)

    async def record_evaluation_result(
        self, tenant_id: str, agent_id: str, version: int, evaluation_result: EvaluationResult
    ) -> None:
        """Persist the EVALUATING stage's outcome onto the version record
        (Section 4.2, Phase 10) — the same "derived artifact" path as
        record_iac_artifact, just for a JSON-blob field this time."""
        await self._require_agent(tenant_id, agent_id)
        if await self._versioner.get(agent_id, version) is None:
            raise VersionNotFoundError(agent_id, version)
        await self._versioner.record_derived_fields(
            agent_id, version, evaluation_result=evaluation_result.model_dump(mode="json")
        )

    async def record_deployment_trigger(
        self, tenant_id: str, agent_id: str, version: int, deployment_id: str, updated_by: str
    ) -> AgentRecord:
        record = await self._require_agent(tenant_id, agent_id)
        if await self._versioner.get(agent_id, version) is None:
            raise VersionNotFoundError(agent_id, version)

        await self._versioner.record_derived_fields(agent_id, version, deployment_id=deployment_id)

        updated_record = record.model_copy(
            update={"status": "DEPLOYING", "updated_by": updated_by, "updated_at": _now()}
        )
        await asyncio.to_thread(
            self._agents_table.put_item, Item=_agent_item(updated_record)
        )
        return updated_record

    async def mark_deployment_blocked(
        self, tenant_id: str, agent_id: str, updated_by: str
    ) -> AgentRecord:
        """Critical security finding → deployment BLOCKED (Phase 9).

        "Previous version remains LIVE": if a version was already ACTIVE
        before this deployment attempt, the agent reverts to ACTIVE (its
        infrastructure was never touched — R03/F2, terraform apply never
        ran). Only an agent with no prior live version becomes BLOCKED
        outright, since there is nothing to fall back to.
        """
        record = await self._require_agent(tenant_id, agent_id)
        new_status: AgentStatus = "ACTIVE" if record.live_version is not None else "BLOCKED"
        updated_record = record.model_copy(
            update={"status": new_status, "updated_by": updated_by, "updated_at": _now()}
        )
        await asyncio.to_thread(
            self._agents_table.put_item, Item=_agent_item(updated_record)
        )
        return updated_record

    async def mark_deployment_active(
        self, tenant_id: str, agent_id: str, live_version: int, updated_by: str
    ) -> AgentRecord:
        """MarkActive (Section 6.2 / Phase 11): HEALTH_CHECK passed — the new
        version now becomes live. R22: the previous version stayed LIVE right
        up to this call; nothing before HEALTH_CHECK's PASS should ever call
        this."""
        record = await self._require_agent(tenant_id, agent_id)
        updated_record = record.model_copy(
            update={
                "status": "ACTIVE",
                "live_version": live_version,
                "updated_by": updated_by,
                "updated_at": _now(),
            }
        )
        await asyncio.to_thread(
            self._agents_table.put_item, Item=_agent_item(updated_record)
        )
        return updated_record

    async def mark_deployment_failed(
        self, tenant_id: str, agent_id: str, updated_by: str
    ) -> AgentRecord:
        """MarkFailed (Section 6.2 / Phase 11): same fallback rule as
        mark_deployment_blocked — a pipeline FAILURE (as opposed to a policy
        BLOCK) still leaves any previously-live version live (R22); only an
        agent with no prior live version becomes FAILED outright."""
        record = await self._require_agent(tenant_id, agent_id)
        new_status: AgentStatus = "ACTIVE" if record.live_version is not None else "FAILED"
        updated_record = record.model_copy(
            update={"status": new_status, "updated_by": updated_by, "updated_at": _now()}
        )
        await asyncio.to_thread(
            self._agents_table.put_item, Item=_agent_item(updated_record)
        )
        return updated_record

    async def list_agents(
        self,
        tenant_id: str,
        status: AgentStatus | None = None,
        limit: int = 50,
        cursor: str | None = None,
    ) -> tuple[list[AgentRecord], str | None]:
        query_kwargs: dict[str, Any] = {
            "KeyConditionExpression": Key("tenant_id").eq(tenant_id),
            "Limit": limit,
        }
        if status is not None:
            query_kwargs["FilterExpression"] = Attr("status").eq(status)
        if cursor is not None:
            query_kwargs["ExclusiveStartKey"] = _decode_cursor(cursor)

        response = await asyncio.to_thread(self._agents_table.query, **query_kwargs)
        records = [
            AgentRecord(**_normalise_agent_item(decimal_to_native(item)))
            for item in response.get("Items", [])
        ]
        next_cursor = None
        if "LastEvaluatedKey" in response:
            next_cursor = _encode_cursor(response["LastEvaluatedKey"])
        return records, next_cursor

    async def list_agents_by_project(self, tenant_id: str, project_id: str) -> list[AgentRecord]:
        """Section 38 — agents scoped to a project. Queries the
        project-index GSI (hash key = project_id only) and filters by
        tenant_id in the same call (R01) since the GSI itself isn't
        tenant-scoped — matches DeploymentStatusStore's deployment-id-index
        pattern."""
        response = await asyncio.to_thread(
            self._agents_table.query,
            IndexName=_PROJECT_INDEX,
            KeyConditionExpression=Key("project_id").eq(project_id),
            FilterExpression=Attr("tenant_id").eq(tenant_id),
        )
        return [
            AgentRecord(**_normalise_agent_item(decimal_to_native(item)))
            for item in response.get("Items", [])
        ]

    async def get_agent_in_project(
        self, tenant_id: str, project_id: str, agent_id: str
    ) -> AgentRecord | None:
        record = await self.get_agent(tenant_id, agent_id)
        if record is None or record.project_id != project_id:
            return None
        return record

    # ── Update / rollback (both create a new version — never overwrite) ─

    async def update_agent(
        self,
        tenant_id: str,
        agent_id: str,
        configuration: AgentConfiguration,
        changed_by: str,
        change_description: str,
    ) -> tuple[AgentRecord, AgentVersionRecord]:
        return await self._create_new_version(
            tenant_id=tenant_id,
            agent_id=agent_id,
            configuration=configuration,
            changed_by=changed_by,
            change_description=change_description,
        )

    async def rollback_agent(
        self,
        tenant_id: str,
        agent_id: str,
        target_version: int,
        reason: str,
        changed_by: str,
    ) -> tuple[AgentRecord, AgentVersionRecord]:
        record = await self._require_agent(tenant_id, agent_id)

        target = await self._versioner.get(agent_id, target_version)
        if target is None:
            raise VersionNotFoundError(agent_id, target_version)
        if target_version == record.current_version:
            raise InvalidRollbackError(
                f"Version {target_version} is already the current version of {agent_id!r}"
            )

        return await self._create_new_version(
            tenant_id=tenant_id,
            agent_id=agent_id,
            configuration=target.configuration,
            changed_by=changed_by,
            change_description=reason,
            rolled_back_from_version=record.current_version,
            existing_record=record,
        )

    async def _create_new_version(
        self,
        tenant_id: str,
        agent_id: str,
        configuration: AgentConfiguration,
        changed_by: str,
        change_description: str,
        rolled_back_from_version: int | None = None,
        existing_record: AgentRecord | None = None,
    ) -> tuple[AgentRecord, AgentVersionRecord]:
        record = existing_record or await self._require_agent(tenant_id, agent_id)

        await self._validate_no_circular_dependency(
            tenant_id, agent_id=agent_id, configuration=configuration
        )

        next_version = record.current_version + 1
        version_record = self._versioner.build_version(
            agent_id=agent_id,
            agent_name=record.name,
            agent_type=record.agent_type,
            version=next_version,
            configuration=configuration,
            changed_by=changed_by,
            change_description=change_description,
            rolled_back_from_version=rolled_back_from_version,
        )
        updated_record = record.model_copy(
            update={
                "current_version": next_version,
                "updated_by": changed_by,
                "updated_at": _now(),
            }
        )

        await self._versioner.write(version_record)
        await asyncio.to_thread(
            self._agents_table.put_item, Item=_agent_item(updated_record)
        )
        return updated_record, version_record

    # ── Delete (soft — DEPRECATED status) ───────────────────────────────

    async def soft_delete_agent(
        self, tenant_id: str, agent_id: str, updated_by: str
    ) -> AgentRecord:
        record = await self._require_agent(tenant_id, agent_id)

        updated_record = record.model_copy(
            update={"status": "DEPRECATED", "updated_by": updated_by, "updated_at": _now()}
        )
        await asyncio.to_thread(
            self._agents_table.put_item, Item=_agent_item(updated_record)
        )
        return updated_record

    # ── Project lifecycle (Section 38.11) — draft/published/deprecated/
    # archived. Distinct from soft_delete_agent above (which sets the
    # unrelated pipeline `status` to DEPRECATED) — these methods only ever
    # touch `project_lifecycle_status`, never `status`/`live_version`.

    async def set_project_draft_after_edit(
        self, tenant_id: str, agent_id: str, actor: str
    ) -> AgentRecord:
        """Section 38.11: "Edit published agent: auto-create new draft
        version. Never mutate a published record in place." Called by the
        API layer right after update_agent() creates the new version —
        applied unconditionally (not only when the prior status was
        "published") so an edited version is never live until explicitly
        (re)published."""
        record = await self._require_agent(tenant_id, agent_id)
        await self._versioner.record_derived_fields(
            agent_id, record.current_version, project_lifecycle_status="draft"
        )
        updated_record = record.model_copy(
            update={"project_lifecycle_status": "draft", "updated_by": actor, "updated_at": _now()}
        )
        await asyncio.to_thread(self._agents_table.put_item, Item=_agent_item(updated_record))
        return updated_record

    async def publish_agent(self, tenant_id: str, agent_id: str, actor: str) -> AgentRecord:
        """Publishes the agent's current version. If a different version was
        previously published, it is marked "deprecated" (no data deleted) —
        matches the "deprecated set automatically on republish" rule."""
        record = await self._require_agent(tenant_id, agent_id)

        previous_published = await self._find_version_with_status(agent_id, "published")
        if previous_published is not None and previous_published.version != record.current_version:
            await self._versioner.record_derived_fields(
                agent_id, previous_published.version, project_lifecycle_status="deprecated"
            )

        await self._versioner.record_derived_fields(
            agent_id, record.current_version, project_lifecycle_status="published"
        )
        updated_record = record.model_copy(
            update={
                "project_lifecycle_status": "published",
                "updated_by": actor,
                "updated_at": _now(),
            }
        )
        await asyncio.to_thread(self._agents_table.put_item, Item=_agent_item(updated_record))
        return updated_record

    async def archive_agent(self, tenant_id: str, agent_id: str, actor: str) -> AgentRecord:
        """Archives the agent. All data is kept — this is never a delete."""
        record = await self._require_agent(tenant_id, agent_id)
        await self._versioner.record_derived_fields(
            agent_id, record.current_version, project_lifecycle_status="archived"
        )
        updated_record = record.model_copy(
            update={
                "project_lifecycle_status": "archived",
                "updated_by": actor,
                "updated_at": _now(),
            }
        )
        await asyncio.to_thread(self._agents_table.put_item, Item=_agent_item(updated_record))
        return updated_record

    async def rollback_project_agent(
        self, tenant_id: str, agent_id: str, target_version: int, actor: str
    ) -> AgentRecord:
        """Section 38.11: "set target version to published, set current
        published to deprecated. No data deleted." — an in-place status
        flip on existing immutable version records plus repointing
        `current_version`, NOT a new version (unlike the pipeline's own
        rollback_agent, which always creates version N+1 per R23)."""
        record = await self._require_agent(tenant_id, agent_id)
        target = await self._versioner.get(agent_id, target_version)
        if target is None:
            raise VersionNotFoundError(agent_id, target_version)
        if target_version == record.current_version:
            raise InvalidRollbackError(
                f"Version {target_version} is already the current version of {agent_id!r}"
            )

        await self._versioner.record_derived_fields(
            agent_id, record.current_version, project_lifecycle_status="deprecated"
        )
        await self._versioner.record_derived_fields(
            agent_id, target_version, project_lifecycle_status="published"
        )
        updated_record = record.model_copy(
            update={
                "current_version": target_version,
                "project_lifecycle_status": "published",
                "updated_by": actor,
                "updated_at": _now(),
            }
        )
        await asyncio.to_thread(self._agents_table.put_item, Item=_agent_item(updated_record))
        return updated_record

    async def hard_delete_agent(self, tenant_id: str, agent_id: str) -> None:
        """Section 38.11: only ever called by the API layer after verifying
        project_lifecycle_status == "archived" and zero references — a
        genuinely irreversible delete of the AgentRecord and every one of
        its AgentVersionRecords."""
        record = await self._require_agent(tenant_id, agent_id)
        versions = await self._versioner.list_all(agent_id)
        for version in versions:
            await self._versioner.delete(agent_id, version.version)
        await asyncio.to_thread(
            self._agents_table.delete_item,
            Key={"tenant_id": tenant_id, "agent_id": record.agent_id},
        )

    async def _find_version_with_status(
        self, agent_id: str, project_lifecycle_status: ProjectLifecycleStatus
    ) -> AgentVersionRecord | None:
        for version in await self._versioner.list_all(agent_id):
            if version.project_lifecycle_status == project_lifecycle_status:
                return version
        return None

    # ── Circular dependency validation (A5) ─────────────────────────────

    async def _validate_no_circular_dependency(
        self, tenant_id: str, agent_id: str | None, configuration: AgentConfiguration
    ) -> None:
        if configuration.orchestration is None or not configuration.orchestration.sub_agents:
            return

        proposed_sub_agents = [ref.agent_id for ref in configuration.orchestration.sub_agents]
        graph = await self._build_sub_agent_graph(tenant_id, exclude_agent_id=agent_id)
        result = self._dependency_validator.validate(
            agent_id=agent_id or "__new_agent__",
            proposed_sub_agents=proposed_sub_agents,
            all_agents=graph,
        )
        if not result.valid:
            raise CircularDependencyError(result.reason or "circular_dependency_detected")

    async def _build_sub_agent_graph(
        self, tenant_id: str, exclude_agent_id: str | None
    ) -> dict[str, list[str]]:
        graph: dict[str, list[str]] = {}
        cursor: str | None = None
        while True:
            records, cursor = await self.list_agents(tenant_id, limit=100, cursor=cursor)
            for record in records:
                if record.agent_id == exclude_agent_id:
                    continue
                version = await self.get_version(record.agent_id, record.current_version)
                sub_agents: list[str] = []
                if version and version.configuration.orchestration:
                    sub_agents = [
                        ref.agent_id for ref in version.configuration.orchestration.sub_agents
                    ]
                graph[record.agent_id] = sub_agents
            if cursor is None:
                break
        return graph
