# S3 buckets (CLAUDE.md A13: "S3 buckets (IaC artifacts, transcripts,
# reports, audit)"). Bucket *contents* are the application's concern
# (app/modules/iac_generator, .../audit, .../monitoring — R24/R25: none of
# this data is ever sent to Panasa); this stage only provisions the buckets
# themselves.

locals {
  s3_buckets = {
    iac_artifacts = "${var.resource_prefix}-iac-artifacts-${local.account_id}"
    transcripts   = "${var.resource_prefix}-transcripts-${local.account_id}"
    reports       = "${var.resource_prefix}-reports-${local.account_id}"
    # instructions_kb_api.md / CLAUDE.md Section 43 — raw source documents
    # for Knowledge Bases, one prefix per KB ({tenant_id}/{kb_id}/raw/).
    # Bedrock's data source (bedrock_kb.tf) reads from this same bucket.
    kb_documents = "${var.resource_prefix}-kb-documents-${local.account_id}"
  }
}

resource "aws_s3_bucket" "app" {
  for_each = local.s3_buckets
  bucket   = each.value
}

resource "aws_s3_bucket_versioning" "app" {
  for_each = aws_s3_bucket.app
  bucket   = each.value.id
  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "app" {
  for_each = aws_s3_bucket.app
  bucket   = each.value.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm     = "aws:kms"
      kms_master_key_id = var.kms_key_arn
    }
    bucket_key_enabled = true
  }
}

resource "aws_s3_bucket_public_access_block" "app" {
  for_each                = aws_s3_bucket.app
  bucket                  = each.value.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_lifecycle_configuration" "transcripts" {
  bucket = aws_s3_bucket.app["transcripts"].id
  rule {
    id     = "expire-per-tenant-retention"
    status = "Enabled"
    # Section 4.10: default 90 days, but this is enforced per-record via
    # DynamoDB TTL (expires_at) against panasa-transcripts — this bucket
    # lifecycle rule is a coarse backstop for the S3-overflow path
    # (transcripts too large for a single DynamoDB item), not the primary
    # retention mechanism.
    expiration {
      days = 365
    }
    noncurrent_version_expiration {
      noncurrent_days = 30
    }
  }
  depends_on = [aws_s3_bucket_versioning.app]
}

# ── Audit bucket — WORM (Section 14 Phase 14 / F9's audit trail) ────────
# Object Lock in COMPLIANCE mode here is deliberate and different from
# Stage 0's state bucket: audit logs are *meant* to be genuinely
# tamper-proof, including against the account root — that's the entire
# point of a WORM audit trail. The operational risk Stage 0 avoided (an
# unrecoverable mistake on infrastructure the platform depends on to
# function) doesn't apply to a write-once log the platform never reads back
# to make decisions.

resource "aws_s3_bucket" "audit" {
  bucket              = "${var.resource_prefix}-audit-${local.account_id}"
  object_lock_enabled = true
}

resource "aws_s3_bucket_versioning" "audit" {
  bucket = aws_s3_bucket.audit.id
  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_object_lock_configuration" "audit" {
  bucket = aws_s3_bucket.audit.id
  rule {
    default_retention {
      mode  = "COMPLIANCE"
      years = 7
    }
  }
  depends_on = [aws_s3_bucket_versioning.audit]
}

resource "aws_s3_bucket_server_side_encryption_configuration" "audit" {
  bucket = aws_s3_bucket.audit.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm     = "aws:kms"
      kms_master_key_id = var.kms_key_arn
    }
    bucket_key_enabled = true
  }
}

resource "aws_s3_bucket_public_access_block" "audit" {
  bucket                  = aws_s3_bucket.audit.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}
