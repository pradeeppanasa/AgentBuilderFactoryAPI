# Self-hosted Langfuse (CLAUDE.md A13/F7: "Langfuse (self-hosted, ECS
# Fargate)", within the customer VPC). Deliberately minimal — see
# variables.tf's deploy_langfuse description and bootstrap/README.md:
# no ClickHouse/object storage, Postgres-backed only, and not exposed
# through the public ALB (internal-only, reached by the Runtime via AWS
# Cloud Map service discovery). Fine for prototype-scale tracing; a real
# production deployment needs Langfuse's documented full stack.

resource "aws_service_discovery_private_dns_namespace" "internal" {
  count = var.deploy_langfuse ? 1 : 0
  name  = "${local.name_prefix}.internal"
  vpc   = aws_vpc.main.id
}

resource "aws_service_discovery_service" "langfuse_web" {
  count = var.deploy_langfuse ? 1 : 0
  name  = "langfuse-web"

  dns_config {
    namespace_id = aws_service_discovery_private_dns_namespace.internal[0].id
    dns_records {
      ttl  = 10
      type = "A"
    }
    routing_policy = "MULTIVALUE"
  }

  health_check_custom_config {
    failure_threshold = 1
  }
}

resource "random_password" "langfuse_nextauth_secret" {
  count   = var.deploy_langfuse ? 1 : 0
  length  = 32
  special = false
}

resource "random_password" "langfuse_salt" {
  count   = var.deploy_langfuse ? 1 : 0
  length  = 32
  special = false
}

resource "aws_secretsmanager_secret" "langfuse_nextauth_secret" {
  count      = var.deploy_langfuse ? 1 : 0
  name       = "${var.resource_prefix}/langfuse-nextauth-secret"
  kms_key_id = var.kms_key_arn
}

resource "aws_secretsmanager_secret_version" "langfuse_nextauth_secret" {
  count         = var.deploy_langfuse ? 1 : 0
  secret_id     = aws_secretsmanager_secret.langfuse_nextauth_secret[0].id
  secret_string = random_password.langfuse_nextauth_secret[0].result
}

resource "aws_secretsmanager_secret" "langfuse_salt" {
  count      = var.deploy_langfuse ? 1 : 0
  name       = "${var.resource_prefix}/langfuse-salt"
  kms_key_id = var.kms_key_arn
}

resource "aws_secretsmanager_secret_version" "langfuse_salt" {
  count         = var.deploy_langfuse ? 1 : 0
  secret_id     = aws_secretsmanager_secret.langfuse_salt[0].id
  secret_string = random_password.langfuse_salt[0].result
}

resource "aws_cloudwatch_log_group" "langfuse" {
  count             = var.deploy_langfuse ? 1 : 0
  name              = "/ecs/${local.name_prefix}/langfuse"
  retention_in_days = var.log_retention_days
  kms_key_id        = var.kms_key_arn
}

locals {
  langfuse_secrets = var.deploy_langfuse ? [
    { name = "DATABASE_URL", valueFrom = aws_secretsmanager_secret.langfuse_database_url[0].arn },
    { name = "NEXTAUTH_SECRET", valueFrom = aws_secretsmanager_secret.langfuse_nextauth_secret[0].arn },
    { name = "SALT", valueFrom = aws_secretsmanager_secret.langfuse_salt[0].arn },
  ] : []
}

resource "aws_ecs_task_definition" "langfuse_web" {
  count                    = var.deploy_langfuse ? 1 : 0
  family                   = "${local.name_prefix}-langfuse-web"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = 512
  memory                   = 1024
  execution_role_arn       = aws_iam_role.ecs_task_execution.arn

  container_definitions = jsonencode([
    {
      name         = "langfuse-web"
      image        = "${var.ecr_repository_urls["langfuse-web"]}:latest"
      essential    = true
      portMappings = [{ containerPort = 3000, protocol = "tcp" }]
      environment = [
        { name = "NEXTAUTH_URL", value = "http://langfuse-web.${aws_service_discovery_private_dns_namespace.internal[0].name}:3000" },
        { name = "TELEMETRY_ENABLED", value = "false" },
      ]
      secrets = local.langfuse_secrets
      logConfiguration = {
        logDriver = "awslogs"
        options = {
          "awslogs-group"         = aws_cloudwatch_log_group.langfuse[0].name
          "awslogs-region"        = var.aws_region
          "awslogs-stream-prefix" = "web"
        }
      }
    }
  ])
}

resource "aws_ecs_service" "langfuse_web" {
  count           = var.deploy_langfuse ? 1 : 0
  name            = "langfuse-web"
  cluster         = aws_ecs_cluster.main.id
  task_definition = aws_ecs_task_definition.langfuse_web[0].arn
  desired_count   = 1
  launch_type     = "FARGATE"

  network_configuration {
    subnets         = [for s in aws_subnet.private : s.id]
    security_groups = [aws_security_group.ecs_service.id]
  }

  service_registries {
    registry_arn = aws_service_discovery_service.langfuse_web[0].arn
  }
}

resource "aws_ecs_task_definition" "langfuse_worker" {
  count                    = var.deploy_langfuse ? 1 : 0
  family                   = "${local.name_prefix}-langfuse-worker"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = 512
  memory                   = 1024
  execution_role_arn       = aws_iam_role.ecs_task_execution.arn

  container_definitions = jsonencode([
    {
      name      = "langfuse-worker"
      image     = "${var.ecr_repository_urls["langfuse-worker"]}:latest"
      essential = true
      environment = [
        { name = "TELEMETRY_ENABLED", value = "false" },
      ]
      secrets = local.langfuse_secrets
      logConfiguration = {
        logDriver = "awslogs"
        options = {
          "awslogs-group"         = aws_cloudwatch_log_group.langfuse[0].name
          "awslogs-region"        = var.aws_region
          "awslogs-stream-prefix" = "worker"
        }
      }
    }
  ])
}

resource "aws_ecs_service" "langfuse_worker" {
  count           = var.deploy_langfuse ? 1 : 0
  name            = "langfuse-worker"
  cluster         = aws_ecs_cluster.main.id
  task_definition = aws_ecs_task_definition.langfuse_worker[0].arn
  desired_count   = 1
  launch_type     = "FARGATE"

  network_configuration {
    subnets         = [for s in aws_subnet.private : s.id]
    security_groups = [aws_security_group.ecs_service.id]
  }
}
