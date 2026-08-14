"""Connector catalog models (CLAUDE.md Section 4.5 / Section 5.4 / Phase 12).

Global (Panasa-provided) connectors and tenant-custom connectors share the
same panasa-connectors table and record shape; only `is_global` and which
tenant_id partition they live under differ. Global connectors are stored
under the sentinel tenant_id "GLOBAL" (mirroring Section 4.9's skills table
SK="GLOBAL" convention — connectors doesn't have a spare key slot for that,
so it uses the partition key instead). No tenant can ever create a record
under that partition — app.modules.connectors.catalog.ConnectorCatalogStore
only writes it via its own seed_global_connectors(), never from a
tenant-facing API call.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

GLOBAL_TENANT_ID = "GLOBAL"

ExecutorType = Literal["http", "lambda", "sql", "mcp"]


class ConnectorRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tenant_id: str
    connector_id: str

    name: str
    executor_type: ExecutorType
    description: str
    input_schema: dict[str, Any] = Field(default_factory=dict)
    output_schema: dict[str, Any] = Field(default_factory=dict)
    endpoint_template: str | None = None
    credentials_required: list[str] = Field(default_factory=list)
    is_global: bool = False

    created_by: str
    created_at: str
