output "account_id" {
  value = local.account_id
}

output "region" {
  value = data.aws_region.current.name
}

output "state_bucket" {
  description = "Consumed by stage1/backend.tf via -backend-config (see bootstrap/README.md)."
  value       = aws_s3_bucket.terraform_state.bucket
}

output "state_bucket_arn" {
  value = aws_s3_bucket.terraform_state.arn
}

output "lock_table" {
  description = "Consumed by stage1/backend.tf via -backend-config."
  value       = aws_dynamodb_table.terraform_lock.name
}

output "kms_key_id" {
  description = "Consumed by stage1/backend.tf via -backend-config."
  value       = aws_kms_key.panasa.key_id
}

output "kms_key_arn" {
  value = aws_kms_key.panasa.arn
}

output "ecr_repository_urls" {
  value = { for name, repo in aws_ecr_repository.mirrored : name => repo.repository_url }
}

output "agent_builder_runtime_role_arn" {
  value = aws_iam_role.agent_builder_runtime.arn
}

output "deployment_role_arn" {
  value = aws_iam_role.deployment.arn
}

output "terraform_execution_role_arn" {
  value = aws_iam_role.terraform_execution.arn
}
