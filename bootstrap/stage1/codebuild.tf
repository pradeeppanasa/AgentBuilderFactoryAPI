# CodeBuild projects — 5 security scans + evaluation + terraform
# validate/plan/apply (Section 6.2 / 8 / 9 / 10 / F1). Names match exactly
# what the Python orchestration modules expect:
#   app.modules.security.scanner.codebuild_project_name()
#   app.modules.evaluation.evaluator.codebuild_project_name()
# terraform-{validate,plan,apply} have no Python-side name constant (the
# Runtime never starts them directly — Step Functions does, via the ASL
# template below) so "panasa-terraform-*" is this stage's own convention.

locals {
  codebuild_source_type = {
    github     = "GITHUB"
    gitlab     = "GITLAB"
    bitbucket  = "BITBUCKET"
    codecommit = "CODECOMMIT"
  }[var.git_provider]

  codebuild_projects = {
    "${var.resource_prefix}-security-sast" = {
      buildspec = "codebuild/sast-buildspec.yml"
    }
    "${var.resource_prefix}-security-secret-scan" = {
      buildspec = "codebuild/secret-scan-buildspec.yml"
    }
    "${var.resource_prefix}-security-dependency-scan" = {
      buildspec = "codebuild/dependency-scan-buildspec.yml"
    }
    "${var.resource_prefix}-security-iac-scan" = {
      buildspec = "codebuild/iac-scan-buildspec.yml"
    }
    "${var.resource_prefix}-security-container-scan" = {
      buildspec = "codebuild/container-scan-buildspec.yml"
    }
    "${var.resource_prefix}-evaluation" = {
      buildspec = "codebuild/evaluation-buildspec.yml"
    }
    "${var.resource_prefix}-terraform-validate" = {
      buildspec = "codebuild/terraform-validate-buildspec.yml"
    }
    "${var.resource_prefix}-terraform-plan" = {
      buildspec = "codebuild/terraform-plan-buildspec.yml"
    }
    "${var.resource_prefix}-terraform-apply" = {
      buildspec = "codebuild/terraform-apply-buildspec.yml"
    }
  }
}

# Registers the git PAT with CodeBuild so the GITHUB/GITLAB/BITBUCKET source
# types below can clone var.git_repo_url. Not needed (and not created) for
# codecommit, which authenticates via the project's own IAM role instead.
resource "aws_codebuild_source_credential" "git" {
  count       = var.git_provider != "codecommit" && var.git_token_value != "" ? 1 : 0
  auth_type   = "PERSONAL_ACCESS_TOKEN"
  server_type = upper(var.git_provider)
  token       = var.git_token_value
}

resource "aws_cloudwatch_log_group" "codebuild" {
  for_each          = local.codebuild_projects
  name              = "/codebuild/${each.key}"
  retention_in_days = var.log_retention_days
  kms_key_id        = var.kms_key_arn
}

resource "aws_codebuild_project" "pipeline" {
  for_each = local.codebuild_projects

  name         = each.key
  service_role = var.deployment_role_arn
  build_timeout = 30

  artifacts {
    type = "NO_ARTIFACTS"
  }

  environment {
    type                        = "LINUX_CONTAINER"
    compute_type                = "BUILD_GENERAL1_SMALL"
    image                       = "aws/codebuild/standard:7.0"
    image_pull_credentials_type = "CODEBUILD"

    environment_variable {
      name  = "DYNAMODB_DEPLOYMENTS_TABLE"
      value = aws_dynamodb_table.app["panasa-deployments"].name
    }
  }

  source {
    type      = local.codebuild_source_type
    location  = var.git_repo_url
    buildspec = each.value.buildspec
  }

  logs_config {
    cloudwatch_logs {
      group_name = aws_cloudwatch_log_group.codebuild[each.key].name
    }
  }

  depends_on = [aws_codebuild_source_credential.git]
}
