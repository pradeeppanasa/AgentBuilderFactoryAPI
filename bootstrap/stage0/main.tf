# Panasa Agent Factory — Bootstrap Stage 0 (CLAUDE.md Section 9 / A13 / F13).
#
# Local state only. This stage's entire job is to create the remote backend
# (S3 + DynamoDB lock) that Stage 1 then uses — so Stage 0 itself cannot
# depend on that backend. Run once per AWS account (prototype: Panasa's;
# enterprise: the customer's — R03/R04, Panasa never holds the credentials
# that run this).

terraform {
  required_version = ">= 1.9"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }
}

provider "aws" {
  region = var.aws_region

  default_tags {
    tags = merge(var.tags, {
      "panasa:managed-by"  = "terraform"
      "panasa:bootstrap"   = "stage0"
      "panasa:environment" = var.environment
    })
  }
}

data "aws_caller_identity" "current" {}
data "aws_region" "current" {}

locals {
  account_id  = data.aws_caller_identity.current.account_id
  name_prefix = "${var.resource_prefix}-${var.environment}"
}

# ── KMS — encryption at rest for state, DynamoDB, secrets, logs ────────────
#
# The key policy delegates to IAM (the standard "Enable IAM User
# Permissions" statement) rather than naming specific role ARNs here —
# naming Stage 0's own IAM roles in this policy would create a dependency
# cycle (key -> role -> key, since those roles also need kms:Decrypt on this
# key). IAM policies attached to the roles below grant the actual access.

resource "aws_kms_key" "panasa" {
  description             = "${local.name_prefix} — Terraform state, DynamoDB, secrets, and log encryption"
  deletion_window_in_days = var.kms_deletion_window_days
  enable_key_rotation     = true

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid       = "EnableIamUserPermissions"
        Effect    = "Allow"
        Principal = { AWS = "arn:aws:iam::${local.account_id}:root" }
        Action    = "kms:*"
        Resource  = "*"
      },
      {
        Sid    = "AllowCloudWatchLogsEncryption"
        Effect = "Allow"
        Principal = {
          Service = "logs.${data.aws_region.current.name}.amazonaws.com"
        }
        Action = [
          "kms:Encrypt*",
          "kms:Decrypt*",
          "kms:ReEncrypt*",
          "kms:GenerateDataKey*",
          "kms:Describe*",
        ]
        Resource = "*"
        Condition = {
          ArnLike = {
            "kms:EncryptionContext:aws:logs:arn" = "arn:aws:logs:${data.aws_region.current.name}:${local.account_id}:*"
          }
        }
      },
    ]
  })
}

resource "aws_kms_alias" "panasa" {
  name          = "alias/${local.name_prefix}"
  target_key_id = aws_kms_key.panasa.key_id
}

# ── S3 — Terraform state bucket ──────────────────────────────────────────
#
# Object Lock uses GOVERNANCE mode, not COMPLIANCE: COMPLIANCE cannot be
# shortened or bypassed by anyone, including the account root — for a
# bucket this operationally central (every future `terraform apply` reads
# and writes it), an unrecoverable mistake here would be worse than the
# tamper-protection Object Lock is meant to provide. GOVERNANCE still
# protects against accidental/malicious overwrite or deletion; bypassing it
# requires s3:BypassGovernanceRetention, which no role below is granted.

resource "aws_s3_bucket" "terraform_state" {
  bucket              = "${local.name_prefix}-tf-state-${local.account_id}"
  object_lock_enabled = true
}

resource "aws_s3_bucket_versioning" "terraform_state" {
  bucket = aws_s3_bucket.terraform_state.id
  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "terraform_state" {
  bucket = aws_s3_bucket.terraform_state.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm     = "aws:kms"
      kms_master_key_id = aws_kms_key.panasa.arn
    }
    bucket_key_enabled = true
  }
}

resource "aws_s3_bucket_object_lock_configuration" "terraform_state" {
  bucket = aws_s3_bucket.terraform_state.id
  rule {
    default_retention {
      mode = "GOVERNANCE"
      days = var.state_bucket_object_lock_retention_days
    }
  }
  depends_on = [aws_s3_bucket_versioning.terraform_state]
}

resource "aws_s3_bucket_public_access_block" "terraform_state" {
  bucket                  = aws_s3_bucket.terraform_state.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_policy" "terraform_state_tls_only" {
  bucket = aws_s3_bucket.terraform_state.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid       = "DenyInsecureTransport"
        Effect    = "Deny"
        Principal = "*"
        Action    = "s3:*"
        Resource = [
          aws_s3_bucket.terraform_state.arn,
          "${aws_s3_bucket.terraform_state.arn}/*",
        ]
        Condition = {
          Bool = { "aws:SecureTransport" = "false" }
        }
      },
    ]
  })
}

# ── DynamoDB — Terraform state lock table ───────────────────────────────

resource "aws_dynamodb_table" "terraform_lock" {
  name         = "${local.name_prefix}-tf-lock"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "LockID"

  attribute {
    name = "LockID"
    type = "S"
  }

  server_side_encryption {
    enabled     = true
    kms_key_arn = aws_kms_key.panasa.arn
  }

  point_in_time_recovery {
    enabled = true
  }
}

# ── ECR — repositories for Panasa images mirrored into this account ─────
# (F11/F13/A13: after bootstrap, RUNTIME_IMAGE points here — the Panasa
# public registry is never used at runtime again.)

resource "aws_ecr_repository" "mirrored" {
  for_each = toset(var.mirrored_image_names)

  name                 = "${var.resource_prefix}/${each.value}"
  image_tag_mutability = "IMMUTABLE"

  image_scanning_configuration {
    scan_on_push = true
  }

  encryption_configuration {
    encryption_type = "KMS"
    kms_key         = aws_kms_key.panasa.arn
  }
}

resource "aws_ecr_lifecycle_policy" "mirrored" {
  for_each   = aws_ecr_repository.mirrored
  repository = each.value.name

  policy = jsonencode({
    rules = [
      {
        rulePriority = 1
        description  = "Keep the 20 most recent tagged images; expire everything else"
        selection = {
          tagStatus   = "any"
          countType   = "imageCountMoreThan"
          countNumber = 20
        }
        action = { type = "expire" }
      },
    ]
  })
}
