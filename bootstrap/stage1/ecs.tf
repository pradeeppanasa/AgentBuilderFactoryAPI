# ECS Fargate cluster + the two Factory Console/Runtime services (CLAUDE.md
# Section 9: "ECS services: agent-builder-ui, agent-builder-runtime").

resource "aws_ecs_cluster" "main" {
  name = "${local.name_prefix}-cluster"

  setting {
    name  = "containerInsights"
    value = "enabled"
  }
}

# ── Task execution role (shared) — image pull, logs, injected secrets ───
# Distinct from AgentBuilderRuntimeRole (Stage 0's task *role*, the app's
# own AWS permissions): this is the role ECS itself assumes to start the
# container, before any app code runs.

resource "aws_iam_role" "ecs_task_execution" {
  name = "${local.name_prefix}-ecs-task-execution"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "ecs-tasks.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy_attachment" "ecs_task_execution_managed" {
  role       = aws_iam_role.ecs_task_execution.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
}

resource "aws_iam_role_policy" "ecs_task_execution_secrets" {
  name = "${local.name_prefix}-ecs-task-execution-secrets"
  role = aws_iam_role.ecs_task_execution.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "ReadInjectedSecrets"
        Effect   = "Allow"
        Action   = ["secretsmanager:GetSecretValue"]
        Resource = compact([
          aws_secretsmanager_secret.database_url.arn,
          var.deploy_langfuse ? aws_secretsmanager_secret.langfuse_database_url[0].arn : "",
        ])
      },
      {
        Sid      = "DecryptWithPlatformKey"
        Effect   = "Allow"
        Action   = ["kms:Decrypt"]
        Resource = [var.kms_key_arn]
      },
    ]
  })
}

resource "aws_cloudwatch_log_group" "runtime" {
  name              = "/ecs/${local.name_prefix}/agent-builder-runtime"
  retention_in_days = var.log_retention_days
  kms_key_id        = var.kms_key_arn
}

resource "aws_cloudwatch_log_group" "console" {
  name              = "/ecs/${local.name_prefix}/agent-builder-console"
  retention_in_days = var.log_retention_days
  kms_key_id        = var.kms_key_arn
}

# ── Runtime task definition + service ────────────────────────────────────

locals {
  # Referenced both by aws_ecs_task_definition.runtime's own `family`
  # argument and by the ECS_TASK_DEFINITION_FAMILY env var below — can't
  # use aws_ecs_task_definition.runtime.family for the latter, since that
  # would make the task definition depend on itself.
  runtime_task_family = "${local.name_prefix}-agent-builder-runtime"

  runtime_environment = concat(
    [
      { name = "DEPLOYMENT_MODE", value = var.environment },
      { name = "AWS_REGION", value = var.aws_region },
      { name = "AWS_ACCOUNT_ID", value = local.account_id },
      { name = "EVENTBRIDGE_BUS_NAME", value = aws_cloudwatch_event_bus.deployment.name },
      { name = "STEP_FUNCTIONS_ARN", value = aws_sfn_state_machine.deployment.arn },
      { name = "IAC_OUTPUT_BUCKET", value = aws_s3_bucket.app["iac_artifacts"].bucket },
      { name = "GIT_PROVIDER", value = var.git_provider },
      { name = "GIT_REPO_URL", value = var.git_repo_url },
      { name = "GIT_CREDENTIALS_SECRET", value = aws_secretsmanager_secret.git_token.arn },
      { name = "PLATFORM_VERSION", value = var.platform_version },
      { name = "RUNTIME_IMAGE", value = local.runtime_image },
      { name = "JWT_SECRET_ARN", value = aws_secretsmanager_secret.jwt.arn },
      { name = "SECRETS_MANAGER_PREFIX", value = "${var.resource_prefix}/agents" },
      { name = "TELEMETRY_ENABLED", value = tostring(var.telemetry_enabled) },
      { name = "LOG_LEVEL", value = var.log_level },
      { name = "OAUTH_CALLBACK_BASE_URL", value = local.use_tls ? "https://${var.domain_name}" : "http://${aws_lb.main.dns_name}" },
    ],
    # Explicit, not derived from the table name string — app/config.py's
    # field names don't all follow the table name 1:1 (dynamodb_versions_table
    # -> "panasa-agent-versions", not "panasa-versions"), so a generic
    # transform silently gets that one wrong.
    [
      for env_name, table_key in {
        DYNAMODB_AGENTS_TABLE      = "panasa-agents"
        DYNAMODB_VERSIONS_TABLE    = "panasa-agent-versions"
        DYNAMODB_DEPLOYMENTS_TABLE = "panasa-deployments"
        DYNAMODB_CONNECTORS_TABLE  = "panasa-connectors"
        DYNAMODB_SCHEDULES_TABLE   = "panasa-schedules"
        DYNAMODB_CONNECTIONS_TABLE = "panasa-connections"
        DYNAMODB_MEMORY_TABLE      = "panasa-memory"
        DYNAMODB_MCP_SERVERS_TABLE = "panasa-mcp-servers"
        DYNAMODB_SKILLS_TABLE      = "panasa-skills"
        DYNAMODB_TRANSCRIPTS_TABLE = "panasa-transcripts"
        DYNAMODB_REPORTS_TABLE     = "panasa-reports"
      } : { name = env_name, value = aws_dynamodb_table.app[table_key].name }
    ],
    var.environment == "enterprise" ? [{ name = "LICENSE_TOKEN_SECRET_ARN", value = aws_secretsmanager_secret.license_token[0].arn }] : [],
    var.deploy_langfuse ? [{ name = "LANGFUSE_HOST", value = "http://langfuse-web.${aws_service_discovery_private_dns_namespace.internal[0].name}:3000" }] : [],
    # Phase 15 — platform upgrade. ECS_RUNTIME_SERVICE_NAME is the literal
    # "agent-builder-runtime" (matching aws_ecs_service.runtime's own `name`
    # argument below), not a reference to that resource — referencing it
    # here would make this task definition depend on the service that in
    # turn depends on this task definition.
    [
      { name = "PLATFORM_UPGRADE_STATE_MACHINE_ARN", value = aws_sfn_state_machine.platform_upgrade.arn },
      { name = "DYNAMODB_PLATFORM_UPGRADES_TABLE", value = aws_dynamodb_table.platform_upgrades.name },
      { name = "ECS_CLUSTER_NAME", value = aws_ecs_cluster.main.name },
      { name = "ECS_RUNTIME_SERVICE_NAME", value = "agent-builder-runtime" },
      { name = "ECS_TASK_DEFINITION_FAMILY", value = local.runtime_task_family },
      { name = "PLATFORM_HEALTH_CHECK_URL", value = "${local.use_tls ? "https://${var.domain_name}" : "http://${aws_lb.main.dns_name}"}/api/v1/platform/health" },
    ],
  )

  runtime_secrets = concat(
    [{ name = "DATABASE_URL", valueFrom = aws_secretsmanager_secret.database_url.arn }],
  )
}

resource "aws_ecs_task_definition" "runtime" {
  family                   = local.runtime_task_family
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = var.runtime_cpu
  memory                   = var.runtime_memory
  execution_role_arn       = aws_iam_role.ecs_task_execution.arn
  task_role_arn            = var.agent_builder_runtime_role_arn

  container_definitions = jsonencode([
    {
      name      = "agent-builder-runtime"
      image     = local.runtime_image
      essential = true
      portMappings = [{ containerPort = 8000, protocol = "tcp" }]
      environment = local.runtime_environment
      secrets     = local.runtime_secrets
      logConfiguration = {
        logDriver = "awslogs"
        options = {
          "awslogs-group"         = aws_cloudwatch_log_group.runtime.name
          "awslogs-region"        = var.aws_region
          "awslogs-stream-prefix" = "runtime"
        }
      }
    }
  ])
}

resource "aws_ecs_service" "runtime" {
  name            = "agent-builder-runtime"
  cluster         = aws_ecs_cluster.main.id
  task_definition = aws_ecs_task_definition.runtime.arn
  desired_count   = var.runtime_desired_count
  launch_type     = "FARGATE"

  # Explicit rolling-update bounds for Section 15's "previous version stays
  # LIVE during all deployments" / Section 15's "zero-downtime deploy" —
  # these match AWS's own REPLICA-service defaults, stated outright rather
  # than left implicit, since Phase 15's platform upgrade pipeline relies on
  # this exact behaviour (old tasks keep serving until new ones are healthy).
  deployment_minimum_healthy_percent = 100
  deployment_maximum_percent         = 200

  network_configuration {
    subnets         = [for s in aws_subnet.private : s.id]
    security_groups = [aws_security_group.ecs_service.id]
  }

  load_balancer {
    target_group_arn = aws_lb_target_group.runtime.arn
    container_name    = "agent-builder-runtime"
    container_port    = 8000
  }

  depends_on = [aws_lb_listener.http]
}

# ── Console task definition + service ────────────────────────────────────
# Section 1: "The UI never touches AWS directly" — no task_role_arn, no
# custom IAM policy. It only calls the Runtime's API (VITE_API_URL, routed
# through the same ALB under /api/*).

resource "aws_ecs_task_definition" "console" {
  family                   = "${local.name_prefix}-agent-builder-console"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = var.console_cpu
  memory                   = var.console_memory
  execution_role_arn       = aws_iam_role.ecs_task_execution.arn

  container_definitions = jsonencode([
    {
      name      = "agent-builder-console"
      image     = local.console_image
      essential = true
      portMappings = [{ containerPort = 80, protocol = "tcp" }]
      environment = [
        { name = "VITE_API_URL", value = local.use_tls ? "https://${var.domain_name}" : "http://${aws_lb.main.dns_name}" },
      ]
      logConfiguration = {
        logDriver = "awslogs"
        options = {
          "awslogs-group"         = aws_cloudwatch_log_group.console.name
          "awslogs-region"        = var.aws_region
          "awslogs-stream-prefix" = "console"
        }
      }
    }
  ])
}

resource "aws_ecs_service" "console" {
  name            = "agent-builder-console"
  cluster         = aws_ecs_cluster.main.id
  task_definition = aws_ecs_task_definition.console.arn
  desired_count   = var.console_desired_count
  launch_type     = "FARGATE"

  network_configuration {
    subnets         = [for s in aws_subnet.private : s.id]
    security_groups = [aws_security_group.ecs_service.id]
  }

  load_balancer {
    target_group_arn = aws_lb_target_group.console.arn
    container_name    = "agent-builder-console"
    container_port    = 80
  }

  depends_on = [aws_lb_listener.http]
}
