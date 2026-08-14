# The Step Functions orchestration Lambdas (CLAUDE.md Section 6.2 / Phase
# 11; Phase 15 adds the 6 platform-upgrade handlers) — one shared "fat
# Lambda" deployment package (bootstrap/scripts/build_lambda_package.sh
# must be run before `terraform apply` the first time, and again whenever
# app/ or lambda_handlers/ change), aws_lambda_function resources differing
# only by `handler` (and, for platform_update_service, timeout — see
# local.lambda_timeouts). All of them assume Stage 0's AgentBuilderRuntimeRole
# (var.agent_builder_runtime_role_arn) — see that role's trust policy
# comment in bootstrap/stage0/iam.tf for why it's shared with the ECS
# Runtime rather than duplicated, and for the ECS/ECR/PassRole permissions
# Phase 15 added there for the 6 handlers below.

locals {
  lambda_package_zip = "${path.module}/../../build/lambda_package.zip"

  orchestration_lambdas = {
    validating     = "lambda_handlers.validating.handler"
    change_impact  = "lambda_handlers.change_impact.handler"
    generating_iac = "lambda_handlers.generating_iac.handler"
    policy_check   = "lambda_handlers.policy_check.handler"
    deploying      = "lambda_handlers.deploying.handler"
    health_check   = "lambda_handlers.health_check.handler"
    mark_active    = "lambda_handlers.mark_active.handler"
    mark_blocked   = "lambda_handlers.mark_blocked.handler"
    mark_failed    = "lambda_handlers.mark_failed.handler"

    # Phase 15 — platform upgrade workflow (step_functions/platform_upgrade_workflow.json)
    platform_pull_image               = "lambda_handlers.platform_pull_image.handler"
    platform_register_task_definition = "lambda_handlers.platform_register_task_definition.handler"
    platform_update_service           = "lambda_handlers.platform_update_service.handler"
    platform_health_check             = "lambda_handlers.platform_health_check.handler"
    platform_mark_upgraded             = "lambda_handlers.platform_mark_upgraded.handler"
    platform_mark_upgrade_failed       = "lambda_handlers.platform_mark_upgrade_failed.handler"
  }

  # platform_update_service polls ECS for up to ~5 minutes
  # (lambda_handlers/platform_update_service.py's _POLL_ATTEMPTS *
  # _POLL_DELAY_SECONDS) — needs headroom beyond the 60s default every
  # other handler here is fine with.
  lambda_timeouts = {
    platform_update_service = 360
  }
}

resource "aws_cloudwatch_log_group" "lambda" {
  for_each          = local.orchestration_lambdas
  name              = "/aws/lambda/${var.resource_prefix}-${replace(each.key, "_", "-")}"
  retention_in_days = var.log_retention_days
  kms_key_id        = var.kms_key_arn
}

resource "aws_lambda_function" "orchestration" {
  for_each = local.orchestration_lambdas

  function_name = "${var.resource_prefix}-${replace(each.key, "_", "-")}"
  role          = var.agent_builder_runtime_role_arn
  handler       = each.value
  runtime       = "python3.12"
  timeout       = lookup(local.lambda_timeouts, each.key, 60)
  memory_size   = 512

  filename         = local.lambda_package_zip
  source_code_hash = filebase64sha256(local.lambda_package_zip)

  environment {
    variables = {
      DEPLOYMENT_MODE           = var.environment
      AWS_ACCOUNT_ID            = local.account_id
      DYNAMODB_AGENTS_TABLE     = aws_dynamodb_table.app["panasa-agents"].name
      DYNAMODB_VERSIONS_TABLE   = aws_dynamodb_table.app["panasa-agent-versions"].name
      DYNAMODB_DEPLOYMENTS_TABLE = aws_dynamodb_table.app["panasa-deployments"].name
      IAC_OUTPUT_BUCKET         = aws_s3_bucket.app["iac_artifacts"].bucket
      GIT_PROVIDER              = var.git_provider
      GIT_REPO_URL              = var.git_repo_url
      GIT_CREDENTIALS_SECRET    = aws_secretsmanager_secret.git_token.arn
      PLATFORM_VERSION          = var.platform_version
      SECRETS_MANAGER_PREFIX    = "${var.resource_prefix}/agents"
      # Phase 14 gap closed alongside Phase 15's edits to this file — the
      # "block" audit event (policy_check.py) needs this to actually write.
      AUDIT_S3_BUCKET           = aws_s3_bucket.audit.bucket

      # Phase 15 — only platform_*.py read these, but a shared env block is
      # simpler than a second near-duplicate aws_lambda_function resource.
      DYNAMODB_PLATFORM_UPGRADES_TABLE = aws_dynamodb_table.platform_upgrades.name
      ECS_CLUSTER_NAME                 = aws_ecs_cluster.main.name
      ECS_RUNTIME_SERVICE_NAME         = aws_ecs_service.runtime.name
      ECS_TASK_DEFINITION_FAMILY       = aws_ecs_task_definition.runtime.family
      RUNTIME_IMAGE                    = local.runtime_image
      PLATFORM_HEALTH_CHECK_URL        = "${local.use_tls ? "https://${var.domain_name}" : "http://${aws_lb.main.dns_name}"}/api/v1/platform/health"
    }
  }

  logging_config {
    log_format = "Text"
    log_group  = aws_cloudwatch_log_group.lambda[each.key].name
  }
}
