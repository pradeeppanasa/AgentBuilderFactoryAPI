# Secret *shells* only — Section 11, Rule #3: "Never put secrets in
# Terraform source files." This creates the Secrets Manager resources so
# their ARNs exist and are stable (for JWT_SECRET_ARN / GIT_CREDENTIALS_SECRET
# / LICENSE_TOKEN_SECRET_ARN env vars below), but sets no version/value —
# populate these out-of-band after apply (see bootstrap/README.md), the
# same way scripts/seed_local_secrets.sh does for local dev.
#
# database_url / db_master_password (rds.tf) are the one exception: their
# *value* is Terraform-generated (random_password), not a real external
# secret being typed into Terraform source, and the value never appears in
# a .tf file — only in state, which is itself encrypted (Stage 0's KMS key)
# and access-controlled (DeploymentRole/TerraformExecutionRole only).

resource "aws_secretsmanager_secret" "jwt" {
  name       = var.jwt_secret_name
  kms_key_id = var.kms_key_arn
}

resource "aws_secretsmanager_secret" "git_token" {
  name       = var.git_credentials_secret_name
  kms_key_id = var.kms_key_arn
}

# The one deliberate exception to "no secret values in Terraform source":
# the *value* comes from var.git_token_value, an apply-time input (-var /
# TF_VAR_), never written into a .tf/.tfvars file — see that variable's
# description. Skipped entirely for codecommit, which needs no token.
resource "aws_secretsmanager_secret_version" "git_token" {
  count         = var.git_token_value != "" ? 1 : 0
  secret_id     = aws_secretsmanager_secret.git_token.id
  secret_string = var.git_token_value
}

resource "aws_secretsmanager_secret" "license_token" {
  count      = var.environment == "enterprise" ? 1 : 0
  name       = var.license_token_secret_name
  kms_key_id = var.kms_key_arn
}
