# Step Functions state machine (CLAUDE.md Section 6.2 / F1) — renders the
# existing step_functions/deployment_workflow.json template with this
# stage's real Lambda ARNs and CodeBuild project names. The ASL file itself
# is the single source of truth for pipeline structure; nothing about the
# 12-stage flow is duplicated here.

resource "aws_cloudwatch_log_group" "step_functions" {
  name              = "/aws/states/${local.name_prefix}-deployment"
  retention_in_days = var.log_retention_days
  kms_key_id        = var.kms_key_arn
}

resource "aws_sfn_state_machine" "deployment" {
  name     = "${var.resource_prefix}-deployment"
  role_arn = var.deployment_role_arn
  type     = "STANDARD"

  definition = templatefile("${path.module}/../../step_functions/deployment_workflow.json", {
    validating_lambda_arn     = aws_lambda_function.orchestration["validating"].arn
    change_impact_lambda_arn  = aws_lambda_function.orchestration["change_impact"].arn
    generating_iac_lambda_arn = aws_lambda_function.orchestration["generating_iac"].arn
    policy_check_lambda_arn   = aws_lambda_function.orchestration["policy_check"].arn
    deploying_lambda_arn      = aws_lambda_function.orchestration["deploying"].arn
    health_check_lambda_arn   = aws_lambda_function.orchestration["health_check"].arn
    mark_active_lambda_arn    = aws_lambda_function.orchestration["mark_active"].arn
    mark_blocked_lambda_arn   = aws_lambda_function.orchestration["mark_blocked"].arn
    mark_failed_lambda_arn    = aws_lambda_function.orchestration["mark_failed"].arn

    sast_codebuild_project              = aws_codebuild_project.pipeline["${var.resource_prefix}-security-sast"].name
    secret_scan_codebuild_project       = aws_codebuild_project.pipeline["${var.resource_prefix}-security-secret-scan"].name
    dependency_scan_codebuild_project   = aws_codebuild_project.pipeline["${var.resource_prefix}-security-dependency-scan"].name
    iac_scan_codebuild_project          = aws_codebuild_project.pipeline["${var.resource_prefix}-security-iac-scan"].name
    container_scan_codebuild_project    = aws_codebuild_project.pipeline["${var.resource_prefix}-security-container-scan"].name
    evaluating_codebuild_project        = aws_codebuild_project.pipeline["${var.resource_prefix}-evaluation"].name
    terraform_validate_codebuild_project = aws_codebuild_project.pipeline["${var.resource_prefix}-terraform-validate"].name
    terraform_plan_codebuild_project    = aws_codebuild_project.pipeline["${var.resource_prefix}-terraform-plan"].name
    terraform_apply_codebuild_project   = aws_codebuild_project.pipeline["${var.resource_prefix}-terraform-apply"].name
  })

  logging_configuration {
    log_destination        = "${aws_cloudwatch_log_group.step_functions.arn}:*"
    include_execution_data = true
    level                  = "ALL"
  }
}

# Phase 15 — platform upgrade workflow. Started directly via
# stepfunctions:StartExecution from POST /api/v1/platform/upgrade
# (app.modules.platform.upgrade_orchestrator) — no EventBridge indirection,
# see that module's docstring for why. Reuses var.deployment_role_arn: its
# trust policy already allows states.amazonaws.com (Stage 0), and its
# lambda:InvokeFunction statement is already wildcarded to
# "${var.resource_prefix}-*", which these 6 new function names match too.

resource "aws_cloudwatch_log_group" "step_functions_platform_upgrade" {
  name              = "/aws/states/${local.name_prefix}-platform-upgrade"
  retention_in_days = var.log_retention_days
  kms_key_id        = var.kms_key_arn
}

resource "aws_sfn_state_machine" "platform_upgrade" {
  name     = "${var.resource_prefix}-platform-upgrade"
  role_arn = var.deployment_role_arn
  type     = "STANDARD"

  definition = templatefile("${path.module}/../../step_functions/platform_upgrade_workflow.json", {
    pulling_image_lambda_arn               = aws_lambda_function.orchestration["platform_pull_image"].arn
    registering_task_definition_lambda_arn = aws_lambda_function.orchestration["platform_register_task_definition"].arn
    updating_service_lambda_arn            = aws_lambda_function.orchestration["platform_update_service"].arn
    platform_health_check_lambda_arn       = aws_lambda_function.orchestration["platform_health_check"].arn
    mark_upgraded_lambda_arn               = aws_lambda_function.orchestration["platform_mark_upgraded"].arn
    mark_upgrade_failed_lambda_arn         = aws_lambda_function.orchestration["platform_mark_upgrade_failed"].arn
  })

  logging_configuration {
    log_destination        = "${aws_cloudwatch_log_group.step_functions_platform_upgrade.arn}:*"
    include_execution_data = true
    level                  = "ALL"
  }
}
