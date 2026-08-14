#!/usr/bin/env bash
# Seeds LocalStack with what the runtime needs before its first boot:
# the JWT signing secret (Secrets Manager) and the IaC output bucket (S3).
#
# Real deployments provision these via the customer's own bootstrap Terraform
# (Section 9/A13); there is no bootstrap step yet in local dev, so this
# script stands in for it. Run once after `docker-compose up`.
set -euo pipefail

SECRETS_ENDPOINT="${SECRETS_MANAGER_ENDPOINT:-http://localhost:4566}"
S3_ENDPOINT_URL="${S3_ENDPOINT:-http://localhost:4566}"
SECRET_NAME="jwt-secret"
IAC_BUCKET="${IAC_OUTPUT_BUCKET:-panasa-iac-artifacts-local}"

aws --endpoint-url "$SECRETS_ENDPOINT" secretsmanager create-secret \
  --name "$SECRET_NAME" \
  --secret-string "$(openssl rand -base64 48)" \
  2>/dev/null || \
aws --endpoint-url "$SECRETS_ENDPOINT" secretsmanager put-secret-value \
  --secret-id "$SECRET_NAME" \
  --secret-string "$(openssl rand -base64 48)"

echo "Seeded '$SECRET_NAME' in Secrets Manager at $SECRETS_ENDPOINT"
echo "Set JWT_SECRET_ARN in .env to the ARN LocalStack printed above."

aws --endpoint-url "$S3_ENDPOINT_URL" s3 mb "s3://$IAC_BUCKET" 2>/dev/null || true
echo "Ensured IaC output bucket '$IAC_BUCKET' exists at $S3_ENDPOINT_URL"
