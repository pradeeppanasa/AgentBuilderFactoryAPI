# Remote backend pointing at Stage 0's outputs (CLAUDE.md Section 9).
#
# Terraform backend blocks cannot contain variable references — only
# literals, or values supplied at `terraform init` time via
# `-backend-config`. Section 9's own backend.tf sketch (`bucket =
# var.state_bucket`) isn't valid HCL for this reason; this is the correct
# equivalent: an empty/partial backend block, completed by
# bootstrap/scripts/generate_backend_config.sh reading Stage 0's outputs
# into a backend.hcl file that init consumes. See bootstrap/README.md for
# the exact commands.

terraform {
  backend "s3" {
    key     = "agent-builder/terraform.tfstate"
    encrypt = true
    # bucket, region, dynamodb_table, kms_key_id supplied via
    # `terraform init -backend-config=backend.hcl`
  }
}
