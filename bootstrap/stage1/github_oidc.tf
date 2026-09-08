# GitHub OIDC — Sprint 3 Phase 6 (S-04, CLAUDE.md Section 63.1/63.2).
#
# Replaces long-lived AWS_ACCESS_KEY_ID/AWS_SECRET_ACCESS_KEY GitHub
# Secrets (a standing credential leak risk — R04-adjacent, though this
# specific role is CUSTOMER account infrastructure, not Panasa's) with a
# short-lived role GitHub Actions assumes via OIDC token exchange, once
# per workflow run. The role this creates is what every generated
# panasa-deploy.yml (app/modules/iac_generator/cicd_templates.py's
# `_github_actions()`) assumes to run terraform plan/apply against this
# same AWS account.
#
# One-time, account-level setup — same "created once per environment"
# category as every other resource in this file (CLAUDE.md Section
# 45.8's "shared platform infrastructure"), not generated per-agent. An
# AWS account may only have one OIDC provider per unique URL; if this
# customer already has a token.actions.githubusercontent.com provider
# from unrelated CI/CD, set create_github_oidc_provider = false and
# supply its ARN via existing_github_oidc_provider_arn instead of
# failing on EntityAlreadyExists here.

resource "aws_iam_openid_connect_provider" "github" {
  count = var.create_github_oidc_provider ? 1 : 0

  url             = "https://token.actions.githubusercontent.com"
  client_id_list  = ["sts.amazonaws.com"]
  thumbprint_list = ["6938fd4d98bab03faadb97b34396831e3780aea1"]
}

locals {
  github_oidc_provider_arn = var.create_github_oidc_provider ? aws_iam_openid_connect_provider.github[0].arn : var.existing_github_oidc_provider_arn
}

resource "aws_iam_role" "panasa_deploy" {
  # Must match app/modules/iac_generator/cicd_templates.py's
  # _DEPLOY_ROLE_NAME exactly (Python has no Terraform output to read
  # this from — bootstrap is a one-time, human-run step, R03/F0 — so the
  # two are kept in sync by convention, both using the default
  # var.resource_prefix). Deliberately NOT local.name_prefix (which
  # includes -${var.environment}) — the generated workflow doesn't know
  # this stage's environment value, only the fixed role name.
  name = "${var.resource_prefix}-deploy-role"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Federated = local.github_oidc_provider_arn }
      Action    = "sts:AssumeRoleWithWebIdentity"
      Condition = {
        StringEquals = {
          "token.actions.githubusercontent.com:aud" = "sts.amazonaws.com"
        }
        StringLike = {
          # environment:production ties this directly to R50's approval
          # gate (the GitHub Environment "production" required-reviewer
          # check already on every generated terraform_apply job) — a
          # token minted for any other ref/job cannot assume this role
          # at all, gate or no gate.
          "token.actions.githubusercontent.com:sub" = "repo:${var.github_org}/*:environment:production"
        }
      }
    }]
  })
}

resource "aws_iam_role_policy_attachment" "panasa_deploy" {
  role = aws_iam_role.panasa_deploy.name
  # Broad on purpose for this first cut — terraform apply for an
  # arbitrary agent's generated resources (ECS, API Gateway, IAM,
  # Secrets Manager, DynamoDB items, CloudWatch, ...) touches enough
  # distinct services that a hand-scoped policy risks silently breaking
  # a deploy in a way that's hard to diagnose from a GitHub Actions log.
  # Tracked for least-privilege narrowing as its own item (CLAUDE.md
  # Section 65) — a genuinely different scope of work from S-13c's
  # per-agent *runtime* role, which is about the deployed agent's own
  # ECS task permissions, not the CI/CD pipeline's apply-time role.
  policy_arn = "arn:aws:iam::aws:policy/AdministratorAccess"
}
