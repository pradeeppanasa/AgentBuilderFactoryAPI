output "alb_dns_name" {
  value = aws_lb.main.dns_name
}

output "factory_url" {
  value = local.use_tls ? "https://${var.domain_name}" : "http://${aws_lb.main.dns_name}"
}

output "ecs_cluster_name" {
  value = aws_ecs_cluster.main.name
}

output "vpc_id" {
  value = aws_vpc.main.id
}

output "private_subnet_ids" {
  value = [for s in aws_subnet.private : s.id]
}

output "database_endpoint" {
  value     = aws_db_instance.main.endpoint
  sensitive = true
}

output "eventbridge_bus_name" {
  value = aws_cloudwatch_event_bus.deployment.name
}

output "step_functions_arn" {
  value = aws_sfn_state_machine.deployment.arn
}

output "dynamodb_table_names" {
  value = { for name, table in aws_dynamodb_table.app : name => table.name }
}

output "s3_bucket_names" {
  value = merge(
    { for key, bucket in aws_s3_bucket.app : key => bucket.bucket },
    { audit = aws_s3_bucket.audit.bucket },
  )
}

output "codebuild_project_names" {
  value = { for name, project in aws_codebuild_project.pipeline : name => project.name }
}

output "jwt_secret_arn" {
  value = aws_secretsmanager_secret.jwt.arn
}

output "git_credentials_secret_arn" {
  value = aws_secretsmanager_secret.git_token.arn
}

output "runtime_env_summary" {
  description = "Every value the .env.example documents that this stage produced — paste into the Runtime's real environment (ECS already has these baked into its task definition; this is for local/manual reference)."
  value = {
    DYNAMODB_AGENTS_TABLE       = aws_dynamodb_table.app["panasa-agents"].name
    DYNAMODB_VERSIONS_TABLE     = aws_dynamodb_table.app["panasa-agent-versions"].name
    DYNAMODB_DEPLOYMENTS_TABLE  = aws_dynamodb_table.app["panasa-deployments"].name
    EVENTBRIDGE_BUS_NAME        = aws_cloudwatch_event_bus.deployment.name
    STEP_FUNCTIONS_ARN          = aws_sfn_state_machine.deployment.arn
    IAC_OUTPUT_BUCKET           = aws_s3_bucket.app["iac_artifacts"].bucket
    JWT_SECRET_ARN              = aws_secretsmanager_secret.jwt.arn
    GIT_CREDENTIALS_SECRET      = aws_secretsmanager_secret.git_token.arn

    # Phase 15 — platform upgrade
    DYNAMODB_PLATFORM_UPGRADES_TABLE   = aws_dynamodb_table.platform_upgrades.name
    PLATFORM_UPGRADE_STATE_MACHINE_ARN = aws_sfn_state_machine.platform_upgrade.arn
    ECS_CLUSTER_NAME                   = aws_ecs_cluster.main.name
    ECS_RUNTIME_SERVICE_NAME           = "agent-builder-runtime"
    ECS_TASK_DEFINITION_FAMILY         = local.runtime_task_family
    PLATFORM_HEALTH_CHECK_URL          = "${local.use_tls ? "https://${var.domain_name}" : "http://${aws_lb.main.dns_name}"}/api/v1/platform/health"
  }
}

output "platform_upgrade_state_machine_arn" {
  value = aws_sfn_state_machine.platform_upgrade.arn
}
