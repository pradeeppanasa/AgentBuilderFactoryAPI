"""Config diff engine (CLAUDE.md Section 5.2 — GET .../versions/{version}/diff).

Produces the changed/added/removed shape shown in the Section 5.2 example
response, computed generically over the AgentConfiguration JSON shape rather
than hand-coding a case per field. List diffing is index-based (matches the
`tools[1]` style in that example) rather than matched by an id key — good
enough for the common "append/remove a trailing item" case; a reordering of
existing items will show as element-wise "changed" entries instead of a
clean move, which is an acceptable simplification for this phase.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.modules.registry.models import AgentConfiguration


class ChangedField(BaseModel):
    field: str
    from_value: Any = Field(alias="from")
    to_value: Any = Field(alias="to")

    model_config = ConfigDict(populate_by_name=True)


class AddedField(BaseModel):
    field: str
    value: Any


class RemovedField(BaseModel):
    field: str
    value: Any


class ConfigDiff(BaseModel):
    changed: list[ChangedField]
    added: list[AddedField]
    removed: list[RemovedField]


def compute_config_diff(
    from_config: AgentConfiguration | None, to_config: AgentConfiguration
) -> ConfigDiff:
    from_data: dict[str, Any] = from_config.model_dump(mode="json") if from_config else {}
    to_data = to_config.model_dump(mode="json")

    changed: list[ChangedField] = []
    added: list[AddedField] = []
    removed: list[RemovedField] = []
    _diff_value(from_data, to_data, "", changed, added, removed)
    return ConfigDiff(changed=changed, added=added, removed=removed)


def _diff_value(
    from_val: Any,
    to_val: Any,
    path: str,
    changed: list[ChangedField],
    added: list[AddedField],
    removed: list[RemovedField],
) -> None:
    if isinstance(from_val, dict) and isinstance(to_val, dict):
        for key in sorted(set(from_val) | set(to_val)):
            child_path = f"{path}.{key}" if path else key
            if key not in from_val:
                added.append(AddedField(field=child_path, value=to_val[key]))
            elif key not in to_val:
                removed.append(RemovedField(field=child_path, value=from_val[key]))
            else:
                _diff_value(from_val[key], to_val[key], child_path, changed, added, removed)
        return

    if isinstance(from_val, list) and isinstance(to_val, list):
        _diff_list(from_val, to_val, path, changed, added, removed)
        return

    if from_val != to_val:
        changed.append(ChangedField(field=path, **{"from": from_val, "to": to_val}))


def _diff_list(
    from_list: list[Any],
    to_list: list[Any],
    path: str,
    changed: list[ChangedField],
    added: list[AddedField],
    removed: list[RemovedField],
) -> None:
    common = min(len(from_list), len(to_list))
    for i in range(common):
        item_from, item_to = from_list[i], to_list[i]
        child_path = f"{path}[{i}]"
        if isinstance(item_from, dict) and isinstance(item_to, dict):
            _diff_value(item_from, item_to, child_path, changed, added, removed)
        elif item_from != item_to:
            changed.append(ChangedField(field=child_path, **{"from": item_from, "to": item_to}))

    for i in range(common, len(to_list)):
        added.append(AddedField(field=f"{path}[{i}]", value=to_list[i]))
    for i in range(common, len(from_list)):
        removed.append(RemovedField(field=f"{path}[{i}]", value=from_list[i]))
