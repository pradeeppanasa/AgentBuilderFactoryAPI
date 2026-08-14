# IAM roles (CLAUDE.md Section 9 / R21) — the three named in the spec.
# agent_builder_runtime is the ECS *task* role (the Runtime container's own
# AWS permissions) — Stage 1 additionally creates a shared ECS *task
# execution* role (image pull + logs + injected secrets) alongside the ECS
# services themselves, since that role belongs with the cluster that uses
# it. The Console container gets no task role at all (Section 1: "The UI
# never touches AWS directly") — only that shared execution role.

locals {
  panasa_dynamodb_table_arns = [
    for table in [
      "panasa-agents", "panasa-agent-versions", "panasa-deployments",
      "panasa-connectors", "panasa-schedules", "panasa-connections",
      "panasa-memory", "panasa-mcp-servers", "panasa-skills",
      "panasa-transcripts", "panasa-reports",
    ] : "arn:aws:dynamodb:${data.aws_region.current.name}:${local.account_id}:table/${table}*"
  ]
}

# ── AgentBuilderRuntimeRole — the Runtime container's own AWS access ────

resource "aws_iam_role" "agent_builder_runtime" {
  name = "${local.name_prefix}-agent-builder-runtime"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect = "Allow"
      # Trusted by both: the ECS-hosted FastAPI Runtime *and* Stage 1's 9
      # Step Functions Lambda handlers (bootstrap/stage1/lambda.tf) need
      # the exact same permission set below (registry/deployments tables,
      # iac-artifacts bucket, platform secrets, KMS) — one role, two
      # possible assumers, rather than a near-duplicate second role.
      Principal = { Service = ["ecs-tasks.amazonaws.com", "lambda.amazonaws.com"] }
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy" "agent_builder_runtime" {
  name = "${local.name_prefix}-agent-builder-runtime"
  role = aws_iam_role.agent_builder_runtime.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "RegistryAndOperationalTables"
        Effect = "Allow"
        Action = [
          "dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:UpdateItem",
          "dynamodb:DeleteItem", "dynamodb:Query", "dynamodb:Scan",
          "dynamodb:BatchGetItem", "dynamodb:BatchWriteItem",
          "dynamodb:DescribeTable", "dynamodb:CreateTable",
        ]
        Resource = local.panasa_dynamodb_table_arns
      },
      {
        Sid      = "IacArtifactsBucket"
        Effect   = "Allow"
        Action   = ["s3:GetObject", "s3:PutObject", "s3:ListBucket"]
        Resource = ["arn:aws:s3:::${var.resource_prefix}-iac-artifacts-${local.account_id}", "arn:aws:s3:::${var.resource_prefix}-iac-artifacts-${local.account_id}/*"]
      },
      {
        Sid    = "AgentSecretsReadOnly"
        Effect = "Allow"
        Action = ["secretsmanager:GetSecretValue"]
        Resource = [
          "arn:aws:secretsmanager:${data.aws_region.current.name}:${local.account_id}:secret:panasa/agents/*",
          "arn:aws:secretsmanager:${data.aws_region.current.name}:${local.account_id}:secret:jwt-secret*",
          "arn:aws:secretsmanager:${data.aws_region.current.name}:${local.account_id}:secret:git-token*",
          "arn:aws:secretsmanager:${data.aws_region.current.name}:${local.account_id}:secret:panasa/license-token*",
        ]
      },
      {
        Sid      = "DeploymentEventPublishing"
        Effect   = "Allow"
        Action   = ["events:PutEvents"]
        Resource = ["arn:aws:events:${data.aws_region.current.name}:${local.account_id}:event-bus/${var.resource_prefix}-agent-builder"]
      },
      {
        Sid      = "DeploymentTelemetryDecrypt"
        Effect   = "Allow"
        Action   = ["kms:Decrypt", "kms:DescribeKey", "kms:GenerateDataKey"]
        Resource = [aws_kms_key.panasa.arn]
      },
      # ── Phase 15 — platform upgrade (GET/POST /api/v1/platform/version,
      # /upgrade in the ECS-hosted Runtime; the 6 platform_*.py Lambdas —
      # both assume this same role, see the trust policy comment above) ──
      {
        Sid    = "PlatformUpgradeTaskDefinitions"
        Effect = "Allow"
        # AWS does not support resource-level permissions for these two
        # actions — an ECS/IAM constraint, not a scoping choice made here.
        # Every other ECS action below is scoped to the one Runtime service.
        Action   = ["ecs:RegisterTaskDefinition", "ecs:DescribeTaskDefinition"]
        Resource = "*"
      },
      {
        Sid      = "PlatformUpgradeService"
        Effect   = "Allow"
        Action   = ["ecs:DescribeServices", "ecs:UpdateService"]
        Resource = ["arn:aws:ecs:${data.aws_region.current.name}:${local.account_id}:service/${local.name_prefix}-cluster/agent-builder-runtime"]
      },
      {
        Sid      = "PlatformUpgradeEcrRead"
        Effect   = "Allow"
        Action   = ["ecr:DescribeImages", "ecr:BatchGetImage"]
        Resource = ["arn:aws:ecr:${data.aws_region.current.name}:${local.account_id}:repository/${var.resource_prefix}/agent-factory-runtime"]
      },
      {
        Sid      = "PlatformUpgradeStartExecution"
        Effect   = "Allow"
        Action   = ["states:StartExecution"]
        Resource = ["arn:aws:states:${data.aws_region.current.name}:${local.account_id}:stateMachine:${var.resource_prefix}-platform-upgrade"]
      },
      {
        Sid    = "PlatformUpgradePassRole"
        Effect = "Allow"
        Action = ["iam:PassRole"]
        Resource = [
          aws_iam_role.agent_builder_runtime.arn,
          # Stage 1's ecs_task_execution role — doesn't exist yet at Stage 0
          # apply time, so referenced by the exact predictable name Stage 1
          # gives it (bootstrap/stage1/ecs.tf), not a Terraform reference.
          "arn:aws:iam::${local.account_id}:role/${local.name_prefix}-ecs-task-execution",
        ]
      },
    ]
  })
}

# ── DeploymentRole — assumed by CodeBuild + Step Functions ──────────────
#
# This necessarily reaches wider than AgentBuilderRuntimeRole: it's the
# identity that actually runs `terraform apply` for an arbitrary agent's
# generated infrastructure (Lambda tools, IAM roles, ECS sidecars — R20/R21),
# so it needs to create those resource *types*. It is still scoped to
# Panasa-namespaced resource names, not account-wide "*" — a permissions
# boundary (kept out of this bootstrap layer, but recommended before this
# role is used against a production customer account) would tighten this
# further.

resource "aws_iam_role" "deployment" {
  name = "${local.name_prefix}-deployment"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect    = "Allow"
        Principal = { Service = "codebuild.amazonaws.com" }
        Action    = "sts:AssumeRole"
      },
      {
        Effect    = "Allow"
        Principal = { Service = "states.amazonaws.com" }
        Action    = "sts:AssumeRole"
      },
    ]
  })
}

resource "aws_iam_role_policy" "deployment" {
  name = "${local.name_prefix}-deployment"
  role = aws_iam_role.deployment.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "InvokeOrchestrationLambdas"
        Effect   = "Allow"
        Action   = ["lambda:InvokeFunction"]
        Resource = ["arn:aws:lambda:${data.aws_region.current.name}:${local.account_id}:function:${var.resource_prefix}-*"]
      },
      {
        Sid    = "RunCodeBuildStages"
        Effect = "Allow"
        Action = [
          "codebuild:StartBuild", "codebuild:BatchGetBuilds", "codebuild:StopBuild",
        ]
        Resource = ["arn:aws:codebuild:${data.aws_region.current.name}:${local.account_id}:project/${var.resource_prefix}-*"]
      },
      {
        Sid    = "DeploymentStatusAndArtifacts"
        Effect = "Allow"
        Action = [
          "dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:UpdateItem", "dynamodb:Query",
        ]
        Resource = ["arn:aws:dynamodb:${data.aws_region.current.name}:${local.account_id}:table/panasa-deployments*", "arn:aws:dynamodb:${data.aws_region.current.name}:${local.account_id}:table/panasa-agent-versions*"]
      },
      {
        Sid      = "ReadGeneratedIac"
        Effect   = "Allow"
        Action   = ["s3:GetObject", "s3:ListBucket"]
        Resource = ["arn:aws:s3:::${var.resource_prefix}-iac-artifacts-${local.account_id}", "arn:aws:s3:::${var.resource_prefix}-iac-artifacts-${local.account_id}/*"]
      },
      {
        Sid    = "TerraformStateBackend"
        Effect = "Allow"
        Action = [
          "s3:GetObject", "s3:PutObject", "s3:ListBucket",
          "dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:DeleteItem",
        ]
        Resource = [
          aws_s3_bucket.terraform_state.arn,
          "${aws_s3_bucket.terraform_state.arn}/*",
          aws_dynamodb_table.terraform_lock.arn,
        ]
      },
      {
        Sid    = "ManageGeneratedAgentResources"
        Effect = "Allow"
        Action = [
          "lambda:CreateFunction", "lambda:UpdateFunctionCode", "lambda:UpdateFunctionConfiguration",
          "lambda:GetFunction", "lambda:DeleteFunction", "lambda:TagResource",
          "iam:CreateRole", "iam:DeleteRole", "iam:GetRole", "iam:PutRolePolicy",
          "iam:DeleteRolePolicy", "iam:AttachRolePolicy", "iam:DetachRolePolicy",
          "iam:TagRole", "iam:PassRole",
        ]
        Resource = [
          "arn:aws:lambda:${data.aws_region.current.name}:${local.account_id}:function:${var.resource_prefix}-*",
          "arn:aws:iam::${local.account_id}:role/${var.resource_prefix}-*",
        ]
      },
      {
        Sid      = "KmsForStateAndSecrets"
        Effect   = "Allow"
        Action   = ["kms:Decrypt", "kms:DescribeKey", "kms:GenerateDataKey"]
        Resource = [aws_kms_key.panasa.arn]
      },
      {
        Sid      = "TerraformLogGroup"
        Effect   = "Allow"
        Action   = ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"]
        Resource = ["arn:aws:logs:${data.aws_region.current.name}:${local.account_id}:log-group:/aws/codebuild/${var.resource_prefix}-*"]
      },
    ]
  })
}

# ── TerraformExecutionRole — bootstraps the Factory itself (Stage 1) ────
#
# Deliberately broad: standing up the Factory means creating VPCs, ALBs,
# ACM certs, ECS clusters, EventBridge buses, and the two roles above — a
# chicken-and-egg problem no resource-scoped policy solves. This role is
# assumed only by the operator/CI identity running `terraform apply` in
# bootstrap/stage1, never by anything in the running Factory itself.
# `assumable_by_arns` should be narrowed from its default before real use.

resource "aws_iam_role" "terraform_execution" {
  name = "${local.name_prefix}-terraform-execution"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { AWS = length(var.terraform_execution_assumable_by_arns) > 0 ? var.terraform_execution_assumable_by_arns : ["arn:aws:iam::${local.account_id}:root"] }
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy_attachment" "terraform_execution_power_user" {
  role       = aws_iam_role.terraform_execution.name
  policy_arn = "arn:aws:iam::aws:policy/PowerUserAccess"
}

# PowerUserAccess explicitly excludes most IAM management — Stage 1 needs
# to create/update the two roles above (and their trust/inline policies),
# so that narrow slice of IAM access is added back here, scoped to
# Panasa-namespaced role names only.
resource "aws_iam_role_policy" "terraform_execution_scoped_iam" {
  name = "${local.name_prefix}-terraform-execution-iam"
  role = aws_iam_role.terraform_execution.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "ManagePanasaRolesOnly"
        Effect = "Allow"
        Action = [
          "iam:CreateRole", "iam:DeleteRole", "iam:GetRole", "iam:UpdateRole",
          "iam:PutRolePolicy", "iam:DeleteRolePolicy", "iam:GetRolePolicy",
          "iam:AttachRolePolicy", "iam:DetachRolePolicy", "iam:ListRolePolicies",
          "iam:ListAttachedRolePolicies", "iam:TagRole", "iam:PassRole",
        ]
        Resource = "arn:aws:iam::${local.account_id}:role/${var.resource_prefix}-*"
      },
    ]
  })
}
