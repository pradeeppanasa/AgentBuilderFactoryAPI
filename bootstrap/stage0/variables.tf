variable "aws_region" {
  description = "AWS region Stage 0 (and later Stage 1) resources are created in."
  type        = string
  default     = "eu-west-2"
}

variable "resource_prefix" {
  description = "Prefix applied to every Stage 0 resource name. Never hardcode account IDs or resource names elsewhere (Coding Rule #4) — reference this instead."
  type        = string
  default     = "panasa"
}

variable "environment" {
  description = "prototype (Panasa-owned account) or enterprise (customer-owned account) — tagging only, per R01: DEPLOYMENT_MODE changes infra targets, never business logic."
  type        = string
  default     = "prototype"

  validation {
    condition     = contains(["prototype", "enterprise"], var.environment)
    error_message = "environment must be \"prototype\" or \"enterprise\"."
  }
}

variable "kms_deletion_window_days" {
  description = "Waiting period before the Stage 0 KMS key is actually deleted, if ever scheduled for deletion."
  type        = number
  default     = 30
}

variable "state_bucket_object_lock_retention_days" {
  description = "GOVERNANCE-mode Object Lock retention on the Terraform state bucket. GOVERNANCE (not COMPLIANCE) is deliberate: COMPLIANCE is irreversible even for the account root user, which is too risky for a bucket this operationally critical — see main.tf."
  type        = number
  default     = 30
}

variable "mirrored_image_names" {
  description = "ECR repositories created to receive Panasa's mirrored images (bootstrap/scripts/mirror_images.sh, F13/A13). One repository per image family; tags distinguish versions."
  type        = list(string)
  default = [
    "agent-factory-runtime",
    "agent-factory-console",
    "langfuse-web",
    "langfuse-worker",
    "code-sandbox",
  ]
}

variable "terraform_execution_assumable_by_arns" {
  description = "IAM principal ARNs allowed to assume TerraformExecutionRole (e.g. a CI/CD role, or specific admin users/roles). Defaults to the account root, which every principal in the account can already act through — narrow this before running against a real account."
  type        = list(string)
  default     = []
}

variable "tags" {
  description = "Tags applied to every Stage 0 resource."
  type        = map(string)
  default     = {}
}
