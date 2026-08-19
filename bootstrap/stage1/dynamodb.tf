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
      # Section 38 — lets /api/v1/projects/{project_id}/agents/* list agents
      # by project without a tenant-wide scan. project_id is optional (only
      # set for project-scoped agents), so this is a sparse index — see
      # app/modules/registry/store.py's _agent_item() for why project_id
      # must be OMITTED (not written as null) on items that lack it.
      gsis = [
        { name = "project-index", hash_key = "project_id", hash_key_type = "S", range_key = null, range_key_type = null },
      ]
      ttl_attribute = null
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
      # Section 38.3 (2026-08-17) — repurposed for the reusable
      # prompt-capability Skill actually implemented in
      # app/modules/skills/store.py; Section 4.9/29's built-in
      # platform-capability Skill concept (hash_key=skill_id,
      # range_key=scope) was never implemented in this codebase, so
      # there is no real schema to preserve here.
      hash_key       = "tenant_id"
      range_key      = "skill_id"
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
    # Advanced Config (CLAUDE.md Section 37.12) — schemas match
    # app/modules/knowledge_base/store.py, app/modules/guardrails/store.py,
    # and app/modules/playground/store.py's ensure_table() key schemas exactly.
    "panasa-knowledge-bases" = {
      hash_key       = "tenant_id"
      range_key      = "kb_id"
      range_key_type = "S"
      gsis           = []
      ttl_attribute  = null
    }
    "panasa-guardrail-policies" = {
      hash_key       = "tenant_id"
      range_key      = "policy_id"
      range_key_type = "S"
      gsis           = []
      ttl_attribute  = null
    }
    "panasa-playground-sessions" = {
      # Keyed by agent_id, not tenant_id — a playground session is always
      # accessed in the context of one specific agent (mirrors
      # panasa-transcripts' shape above), not queried tenant-wide.
      hash_key       = "agent_id"
      range_key      = "session_id"
      range_key_type = "S"
      gsis           = []
      ttl_attribute  = null
    }
    # Observability — Runs Feature, Phase 1. Schema matches
    # app/modules/runs/store.py's ensure_table() exactly: range key is
    # started_at (ISO 8601, lexicographically sortable), not run_id, so a
    # plain Query with ScanIndexForward=False returns newest-first with no
    # separate index.
    "panasa-runs" = {
      hash_key       = "agent_id"
      range_key      = "started_at"
      range_key_type = "S"
      gsis           = []
      ttl_attribute  = null
    }
    "panasa-bedrock-credentials" = {
      # Section 37.15 (2026-08-16) — STS AssumeRole credential bindings.
      # Schema matches app/modules/bedrock_credentials/store.py's
      # ensure_table() key schema exactly.
      hash_key       = "tenant_id"
      range_key      = "credential_id"
      range_key_type = "S"
      gsis           = []
      ttl_attribute  = null
    }
    # Section 38.2/38.7 — schema matches app/modules/projects/store.py's
    # ensure_table() key schema exactly.
    "panasa-projects" = {
      hash_key       = "tenant_id"
      range_key      = "project_id"
      range_key_type = "S"
      gsis           = []
      ttl_attribute  = null
    }
    # Section 38.7/38.8 — schema matches app/modules/hitl/store.py's
    # ensure_table() key schema exactly.
    "panasa-hitl-reviews" = {
      hash_key       = "tenant_id"
      range_key      = "review_id"
      range_key_type = "S"
      gsis           = []
      ttl_attribute  = null
    }
    # Section 39/R45, R45-7/8 — one settings item per tenant (fixed
    # setting_id="GLOBAL" range key). Schema matches
    # app/modules/platform_settings/store.py's ensure_table() exactly.
    "panasa-platform-settings" = {
      hash_key       = "tenant_id"
      range_key      = "setting_id"
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
