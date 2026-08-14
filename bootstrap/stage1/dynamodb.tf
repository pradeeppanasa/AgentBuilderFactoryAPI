# All 11 application DynamoDB tables (CLAUDE.md Section 4). Table names
# match app/config.py's Settings defaults exactly (dynamodb_agents_table,
# etc.) — panasa-agents, panasa-agent-versions and panasa-deployments'
# schemas are additionally cross-checked against the actual key/GSI names
# the runtime code uses (app/modules/registry/store.py,
# app/modules/deployment/status_store.py); the rest mirror Section 4's
# schema docs directly since their modules aren't built yet.

locals {
  dynamodb_tables = {
    "panasa-agents" = {
      hash_key       = "tenant_id"
      range_key      = "agent_id"
      range_key_type = "S"
      gsis           = []
      ttl_attribute  = null
    }
    "panasa-agent-versions" = {
      hash_key       = "agent_id"
      range_key      = "version"
      range_key_type = "N"
      gsis           = []
      ttl_attribute  = null
    }
    "panasa-deployments" = {
      hash_key       = "agent_id"
      range_key      = "deployment_id"
      range_key_type = "S"
      gsis = [
        { name = "deployment-id-index", hash_key = "deployment_id", hash_key_type = "S", range_key = null, range_key_type = null },
      ]
      ttl_attribute = null
    }
    "panasa-connectors" = {
      hash_key       = "tenant_id"
      range_key      = "connector_id"
      range_key_type = "S"
      gsis           = []
      ttl_attribute  = null
    }
    "panasa-schedules" = {
      hash_key       = "agent_id"
      range_key      = "schedule_id"
      range_key_type = "S"
      gsis = [
        { name = "tenant-index", hash_key = "tenant_id", hash_key_type = "S", range_key = null, range_key_type = null },
      ]
      ttl_attribute = null
    }
    "panasa-connections" = {
      hash_key       = "tenant_id"
      range_key      = "connection_id"
      range_key_type = "S"
      gsis = [
        { name = "provider-index", hash_key = "provider", hash_key_type = "S", range_key = null, range_key_type = null },
      ]
      ttl_attribute = null
    }
    "panasa-memory" = {
      hash_key       = "pk"
      range_key      = "memory_id"
      range_key_type = "S"
      gsis           = []
      ttl_attribute  = "expires_at"
    }
    "panasa-mcp-servers" = {
      hash_key       = "tenant_id"
      range_key      = "server_id"
      range_key_type = "S"
      gsis           = []
      ttl_attribute  = null
    }
    "panasa-skills" = {
      hash_key       = "skill_id"
      range_key      = "scope"
      range_key_type = "S"
      gsis           = []
      ttl_attribute  = null
    }
    "panasa-transcripts" = {
      hash_key       = "agent_id"
      range_key      = "session_id"
      range_key_type = "S"
      gsis = [
        { name = "tenant-date-index", hash_key = "tenant_id", hash_key_type = "S", range_key = "started_at", range_key_type = "S" },
      ]
      ttl_attribute = "expires_at"
    }
    "panasa-reports" = {
      hash_key       = "tenant_id"
      range_key      = "report_id"
      range_key_type = "S"
      gsis           = []
      ttl_attribute  = null
    }
  }
}

resource "aws_dynamodb_table" "app" {
  for_each = local.dynamodb_tables

  name         = each.key
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = each.value.hash_key
  range_key    = each.value.range_key

  attribute {
    name = each.value.hash_key
    type = "S"
  }

  attribute {
    name = each.value.range_key
    type = each.value.range_key_type
  }

  dynamic "attribute" {
    # GSI hash/range attributes that aren't already the table's own
    # hash/range key need declaring too — dedupe so DynamoDB doesn't reject
    # a repeated `attribute` block for the same name (e.g. tenant_id could
    # otherwise appear twice).
    for_each = {
      for attr in distinct(concat(
        [for gsi in each.value.gsis : { name = gsi.hash_key, type = gsi.hash_key_type }],
        [for gsi in each.value.gsis : { name = gsi.range_key, type = gsi.range_key_type } if gsi.range_key != null],
      )) : attr.name => attr
      if attr.name != each.value.hash_key && attr.name != each.value.range_key
    }
    content {
      name = attribute.value.name
      type = attribute.value.type
    }
  }

  dynamic "global_secondary_index" {
    for_each = each.value.gsis
    content {
      name            = global_secondary_index.value.name
      hash_key        = global_secondary_index.value.hash_key
      range_key       = global_secondary_index.value.range_key
      projection_type = "ALL"
    }
  }

  dynamic "ttl" {
    for_each = each.value.ttl_attribute != null ? [each.value.ttl_attribute] : []
    content {
      attribute_name = ttl.value
      enabled        = true
    }
  }

  server_side_encryption {
    enabled     = true
    kms_key_arn = var.kms_key_arn
  }

  point_in_time_recovery {
    enabled = true
  }

  tags = { Name = each.key }
}

# panasa-platform-upgrades (Phase 15) — hash-key only (upgrade_id), no
# tenant partition (see app/modules/platform/upgrade_models.py's docstring
# for why this one genuinely has no tenant dimension) — doesn't fit the
# hash+range template above, so it's its own resource rather than forcing
# an unused range key into that shared shape.
resource "aws_dynamodb_table" "platform_upgrades" {
  # Literal, like every table in local.dynamodb_tables above — matches
  # app/config.py's dynamodb_platform_upgrades_table default exactly,
  # the same way "panasa-agents" etc. are never built from var.resource_prefix.
  name         = "panasa-platform-upgrades"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "upgrade_id"

  attribute {
    name = "upgrade_id"
    type = "S"
  }

  server_side_encryption {
    enabled     = true
    kms_key_arn = var.kms_key_arn
  }

  point_in_time_recovery {
    enabled = true
  }

  tags = { Name = "panasa-platform-upgrades" }
}
