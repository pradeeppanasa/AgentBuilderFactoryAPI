#!/usr/bin/env bash
# Bridges Stage 0's outputs into Stage 1's backend (see stage1/backend.tf's
# header comment for why this exists instead of `bucket = var.state_bucket`
# directly in the backend block — Terraform doesn't allow that).
#
# Run from bootstrap/stage0/ *after* `terraform apply` there:
#   ../scripts/generate_backend_config.sh > ../stage1/backend.hcl
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/../stage0"

cat <<EOF
bucket         = "$(terraform output -raw state_bucket)"
region         = "$(terraform output -raw region)"
dynamodb_table = "$(terraform output -raw lock_table)"
kms_key_id     = "$(terraform output -raw kms_key_id)"
EOF
