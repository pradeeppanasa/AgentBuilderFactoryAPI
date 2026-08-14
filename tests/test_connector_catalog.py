"""Unit tests for app.modules.connectors.catalog (CLAUDE.md Section 4.5)."""

from __future__ import annotations

import boto3
import pytest

from app.config import settings
from app.modules.connectors.catalog import ConnectorCatalogStore
from app.modules.connectors.models import GLOBAL_TENANT_ID


@pytest.fixture
async def store() -> ConnectorCatalogStore:
    dynamodb = boto3.resource("dynamodb", region_name="eu-west-2")
    catalog_store = ConnectorCatalogStore(dynamodb, settings)
    await catalog_store.ensure_table()
    return catalog_store


async def test_list_connectors_is_empty_before_seeding(store: ConnectorCatalogStore) -> None:
    assert await store.list_connectors("tenant-a") == []


async def test_seed_global_connectors_populates_the_three_examples(
    store: ConnectorCatalogStore,
) -> None:
    await store.seed_global_connectors()

    connectors = await store.list_connectors("tenant-a")
    ids = {c.connector_id for c in connectors}
    assert ids == {"jira", "salesforce", "companies-house"}
    assert all(c.is_global for c in connectors)
    assert all(c.tenant_id == GLOBAL_TENANT_ID for c in connectors)


async def test_seed_global_connectors_is_idempotent(store: ConnectorCatalogStore) -> None:
    await store.seed_global_connectors()
    await store.seed_global_connectors()

    connectors = await store.list_connectors("tenant-a")
    assert len(connectors) == 3


async def test_create_connector_is_tenant_scoped_and_never_global(
    store: ConnectorCatalogStore,
) -> None:
    created = await store.create_connector(
        tenant_id="tenant-a",
        name="Internal Ticketing",
        executor_type="http",
        description="Custom internal tool",
        created_by="dev@example.com",
    )
    assert created.is_global is False
    assert created.tenant_id == "tenant-a"

    tenant_a_connectors = await store.list_connectors("tenant-a")
    assert created.connector_id in {c.connector_id for c in tenant_a_connectors}

    tenant_b_connectors = await store.list_connectors("tenant-b")
    assert created.connector_id not in {c.connector_id for c in tenant_b_connectors}


async def test_list_connectors_merges_global_and_tenant(store: ConnectorCatalogStore) -> None:
    await store.seed_global_connectors()
    await store.create_connector(
        tenant_id="tenant-a",
        name="Custom Tool",
        executor_type="http",
        description="desc",
        created_by="dev@example.com",
    )

    connectors = await store.list_connectors("tenant-a")
    assert len(connectors) == 4
    assert {c.name for c in connectors} >= {"Jira", "Salesforce", "Custom Tool"}


async def test_get_connector_finds_tenant_scoped_first_then_global(
    store: ConnectorCatalogStore,
) -> None:
    await store.seed_global_connectors()
    created = await store.create_connector(
        tenant_id="tenant-a",
        name="Custom Tool",
        executor_type="http",
        description="desc",
        created_by="dev@example.com",
    )

    tenant_scoped = await store.get_connector("tenant-a", created.connector_id)
    assert tenant_scoped is not None
    assert tenant_scoped.connector_id == created.connector_id

    global_scoped = await store.get_connector("tenant-a", "jira")
    assert global_scoped is not None
    assert global_scoped.is_global is True


async def test_get_connector_unknown_id_returns_none(store: ConnectorCatalogStore) -> None:
    assert await store.get_connector("tenant-a", "does-not-exist") is None


async def test_get_connector_from_other_tenant_is_not_visible(
    store: ConnectorCatalogStore,
) -> None:
    created = await store.create_connector(
        tenant_id="tenant-a",
        name="Private Tool",
        executor_type="http",
        description="desc",
        created_by="dev@example.com",
    )

    assert await store.get_connector("tenant-b", created.connector_id) is None
