"""Skills catalog — DynamoDB CRUD (CLAUDE.md Section 38.3/38.11)."""

from __future__ import annotations

import asyncio
import re
import uuid
from datetime import UTC, datetime
from typing import Any

from boto3.dynamodb.conditions import Key
from botocore.exceptions import ClientError

from app.config import Settings
from app.modules.skills.models import Skill, SkillStatus, SkillVersionSnapshot
from app.shared.dynamodb_types import decimal_to_native


def _slugify(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return slug or "skill"


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _bump_version(version: str) -> str:
    """"1.0" -> "1.1" (Section 38.11's exact example). Falls back to
    appending ".1" for any version string that isn't a plain major.minor
    pair, rather than raising on a value a user may have hand-edited."""
    parts = version.split(".")
    if len(parts) == 2 and parts[1].isdigit():
        return f"{parts[0]}.{int(parts[1]) + 1}"
    return f"{version}.1"


class SkillNotFoundError(Exception):
    def __init__(self, skill_id: str) -> None:
        self.skill_id = skill_id
        super().__init__(f"Skill {skill_id!r} not found")


def _snapshot(skill: Skill, changed_by: str, change_description: str) -> SkillVersionSnapshot:
    return SkillVersionSnapshot(
        version=skill.version,
        name=skill.name,
        description=skill.description,
        capability=skill.capability,
        prompt_fragment=skill.prompt_fragment,
        input_schema=skill.input_schema,
        output_schema=skill.output_schema,
        changed_by=changed_by,
        change_description=change_description,
        created_at=skill.updated_at,
    )


class SkillStore:
    def __init__(self, dynamodb_resource: Any, settings: Settings) -> None:
        self._dynamodb = dynamodb_resource
        self._settings = settings
        self._table = dynamodb_resource.Table(settings.dynamodb_skills_table)

    async def ensure_table(self) -> None:
        def _create() -> None:
            try:
                table = self._dynamodb.create_table(
                    TableName=self._settings.dynamodb_skills_table,
                    KeySchema=[
                        {"AttributeName": "tenant_id", "KeyType": "HASH"},
                        {"AttributeName": "skill_id", "KeyType": "RANGE"},
                    ],
                    AttributeDefinitions=[
                        {"AttributeName": "tenant_id", "AttributeType": "S"},
                        {"AttributeName": "skill_id", "AttributeType": "S"},
                    ],
                    BillingMode="PAY_PER_REQUEST",
                )
                table.wait_until_exists()
            except ClientError as exc:
                if exc.response["Error"]["Code"] != "ResourceInUseException":
                    raise

        await asyncio.to_thread(_create)

    async def create(
        self,
        tenant_id: str,
        name: str,
        description: str,
        capability: str,
        prompt_fragment: str,
        created_by: str,
        input_schema: dict[str, Any] | None = None,
        output_schema: dict[str, Any] | None = None,
        version: str = "1.0",
    ) -> Skill:
        now = _now()
        skill = Skill(
            tenant_id=tenant_id,
            skill_id=f"{_slugify(name)}-{uuid.uuid4().hex[:6]}",
            name=name,
            description=description,
            capability=capability,
            prompt_fragment=prompt_fragment,
            input_schema=input_schema or {},
            output_schema=output_schema or {},
            version=version,
            status="draft",
            version_history=[],
            created_by=created_by,
            created_at=now,
            updated_by=created_by,
            updated_at=now,
        )
        await asyncio.to_thread(self._table.put_item, Item=skill.model_dump(mode="json"))
        return skill

    async def list_skills(self, tenant_id: str) -> list[Skill]:
        response = await asyncio.to_thread(
            self._table.query, KeyConditionExpression=Key("tenant_id").eq(tenant_id)
        )
        return [Skill(**decimal_to_native(item)) for item in response.get("Items", [])]

    async def get(self, tenant_id: str, skill_id: str) -> Skill | None:
        response = await asyncio.to_thread(
            self._table.get_item, Key={"tenant_id": tenant_id, "skill_id": skill_id}
        )
        item = response.get("Item")
        return Skill(**decimal_to_native(item)) if item else None

    async def update(
        self,
        tenant_id: str,
        skill_id: str,
        updated_by: str,
        change_description: str,
        name: str | None = None,
        description: str | None = None,
        capability: str | None = None,
        prompt_fragment: str | None = None,
        input_schema: dict[str, Any] | None = None,
        output_schema: dict[str, Any] | None = None,
        status: SkillStatus | None = None,
    ) -> Skill:
        """Section 38.11: "Skill edit: create new version record." — the
        prior state is snapshotted into `version_history` before the new
        field values are applied; nothing here ever touches an agent's
        `skill_ids` reference.

        A status-only change (archive/restore/publish, Section 38.11's
        state machine) does NOT bump the version or add a history entry —
        only an actual content edit (name/description/capability/
        prompt_fragment/input_schema/output_schema) does.
        """
        existing = await self.get(tenant_id, skill_id)
        if existing is None:
            raise SkillNotFoundError(skill_id)

        is_content_edit = any(
            field is not None
            for field in (
                name,
                description,
                capability,
                prompt_fragment,
                input_schema,
                output_schema,
            )
        )

        now = _now()
        update_fields: dict[str, Any] = {
            "name": name if name is not None else existing.name,
            "description": description if description is not None else existing.description,
            "capability": capability if capability is not None else existing.capability,
            "prompt_fragment": (
                prompt_fragment if prompt_fragment is not None else existing.prompt_fragment
            ),
            "input_schema": input_schema if input_schema is not None else existing.input_schema,
            "output_schema": (
                output_schema if output_schema is not None else existing.output_schema
            ),
            "status": status if status is not None else existing.status,
            "updated_by": updated_by,
            "updated_at": now,
        }
        if is_content_edit:
            snapshot = _snapshot(existing, updated_by, change_description)
            update_fields["version"] = _bump_version(existing.version)
            update_fields["version_history"] = [*existing.version_history, snapshot]

        updated = existing.model_copy(update=update_fields)
        await asyncio.to_thread(self._table.put_item, Item=updated.model_dump(mode="json"))
        return updated

    async def delete(self, tenant_id: str, skill_id: str) -> None:
        await asyncio.to_thread(
            self._table.delete_item, Key={"tenant_id": tenant_id, "skill_id": skill_id}
        )
