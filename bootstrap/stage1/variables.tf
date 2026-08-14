variable "aws_region" {
  type    = string
  default = "eu-west-2"
}

variable "resource_prefix" {
  type    = string
  default = "panasa"
}

variable "environment" {
  type    = string
  default = "prototype"
  validation {
    condition     = contains(["prototype", "enterprise"], var.environment)
    error_message = "environment must be \"prototype\" or \"enterprise\"."
  }
}

variable "tags" {
  type    = map(string)
  default = {}
}

# ── Stage 0 outputs, passed through explicitly (F0: this stage never reads
# Stage 0's state directly — see bootstrap/README.md) ──────────────────────

variable "kms_key_arn" {
  type = string
}

variable "agent_builder_runtime_role_arn" {
  type = string
}

variable "deployment_role_arn" {
  type = string
}

variable "ecr_repository_urls" {
  description = "map(image name => repository URL), from stage0's ecr_repository_urls output."
  type        = map(string)
}

# ── Networking ───────────────────────────────────────────────────────────

variable "vpc_cidr" {
  type    = string
  default = "10.42.0.0/16"
}

variable "availability_zone_count" {
  type    = number
  default = 2
  validation {
    condition     = var.availability_zone_count >= 2
    error_message = "At least 2 AZs are required for ALB + Fargate service placement."
  }
}

variable "nat_gateway_count" {
  description = "1 = single shared NAT (cheaper, one AZ's egress is a single point of failure). Set to availability_zone_count for one NAT per AZ."
  type        = number
  default     = 1
}

# ── DNS / TLS ─────────────────────────────────────────────────────────────
# Section 9: "ALB + Route 53 + ACM (TLS 1.3)". Both are optional — with
# neither set, the ALB is still created (HTTP listener only) so this stage
# can be exercised without owning a domain; set both to get HTTPS.

variable "domain_name" {
  description = "e.g. factory.customer.com (OAUTH_CALLBACK_BASE_URL's host). Leave empty to skip ACM/Route53 and serve HTTP-only."
  type        = string
  default     = ""
}

variable "route53_zone_id" {
  description = "Existing hosted zone to create the ALB alias record + ACM validation records in. Required if domain_name is set."
  type        = string
  default     = ""
}

# ── Container images ─────────────────────────────────────────────────────

variable "runtime_image" {
  description = "Full image URI for panasa-agent-builder-runtime. Defaults to :latest in the Stage 0 ECR repo — pin this for anything beyond first bring-up."
  type        = string
  default     = ""
}

variable "console_image" {
  description = "Full image URI for panasa-agent-builder-ui. Defaults to :latest in the Stage 0 ECR repo."
  type        = string
  default     = ""
}

variable "runtime_cpu" {
  type    = number
  default = 1024
}

variable "runtime_memory" {
  type    = number
  default = 2048
}

variable "console_cpu" {
  type    = number
  default = 256
}

variable "console_memory" {
  type    = number
  default = 512
}

variable "runtime_desired_count" {
  type    = number
  default = 2
}

variable "console_desired_count" {
  type    = number
  default = 2
}

# ── Git provider (Section 10) ────────────────────────────────────────────

variable "git_provider" {
  type    = string
  default = "github"
  validation {
    condition     = contains(["github", "gitlab", "bitbucket", "codecommit"], var.git_provider)
    error_message = "git_provider must be one of github, gitlab, bitbucket, codecommit."
  }
}

variable "git_repo_url" {
  description = "Customer's Terraform IaC repo (Section 10). Required for git_provider != codecommit."
  type        = string
  default     = ""
}

variable "git_credentials_secret_name" {
  description = "Name of the Secrets Manager secret holding the git PAT/token."
  type        = string
  default     = "git-token"
}

variable "git_token_value" {
  description = <<-EOT
    Git PAT/token, supplied only via -var/TF_VAR_ at apply time (e.g. `TF_VAR_git_token_value=$(cat token.txt) terraform apply`) — never committed to a
    .tf or .tfvars file (Section 11 Rule #3 is about source files, not
    apply-time input). Required for github/gitlab/bitbucket (CodeBuild's
    native source integration needs a registered credential before it can
    clone var.git_repo_url — there's no way around supplying it once,
    up front); ignored for codecommit, which authenticates via IAM instead.
    Stored in Secrets Manager (git_credentials_secret_name) for the
    Runtime's own use (app/modules/git_provider) and used here to register
    aws_codebuild_source_credential.
  EOT
  type        = string
  default     = ""
  sensitive   = true

  validation {
    condition     = var.git_provider == "codecommit" || var.git_token_value != ""
    error_message = "git_token_value is required when git_provider is github, gitlab, or bitbucket."
  }
}

# ── Auth (Phase 3) ────────────────────────────────────────────────────────

variable "jwt_secret_name" {
  type    = string
  default = "jwt-secret"
}

variable "license_token_secret_name" {
  description = "F11 — required in enterprise mode; ignored in prototype (Panasa's own account needs no license check)."
  type        = string
  default     = "panasa/license-token"
}

variable "db_instance_class" {
  description = "RDS instance class backing Phase 3's user-account Postgres (and, at prototype scale, Langfuse's Postgres — see rds.tf). Not itemized in CLAUDE.md Section 9's original resource list; added here because Phase 3 requires a real Postgres to exist somewhere."
  type        = string
  default     = "db.t4g.micro"
}

variable "db_allocated_storage_gb" {
  type    = number
  default = 20
}

# ── Observability ─────────────────────────────────────────────────────────

variable "deploy_langfuse" {
  description = "Stand up a minimal self-hosted Langfuse (web+worker on the mirrored images, backed by the same RDS Postgres instance). Prototype-scale only — production trace volume needs Langfuse's full ClickHouse/object-storage stack, which this bootstrap layer intentionally does not provision. See bootstrap/README.md."
  type        = bool
  default     = true
}

variable "platform_version" {
  type    = string
  default = "1.0.0"
}

variable "telemetry_enabled" {
  type    = bool
  default = false
}

variable "log_level" {
  type    = string
  default = "INFO"
}

variable "log_retention_days" {
  type    = number
  default = 30
}
