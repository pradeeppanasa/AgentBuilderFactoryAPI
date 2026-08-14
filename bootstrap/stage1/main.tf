# Panasa Agent Factory — Bootstrap Stage 1 (CLAUDE.md Section 9 / A13 / F13).
#
# Creates the Factory itself: Console + Runtime on ECS Fargate, all
# DynamoDB tables, EventBridge, Step Functions, CodeBuild. After this
# applies successfully, all future agent deployments are self-service via
# the Factory Console — no further Panasa-controlled Terraform runs (A13).

terraform {
  required_version = ">= 1.9"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
    archive = {
      source  = "hashicorp/archive"
      version = "~> 2.4"
    }
    random = {
      source  = "hashicorp/random"
      version = "~> 3.6"
    }
  }
}

provider "aws" {
  region = var.aws_region

  default_tags {
    tags = merge(var.tags, {
      "panasa:managed-by"  = "terraform"
      "panasa:bootstrap"   = "stage1"
      "panasa:environment" = var.environment
    })
  }
}

data "aws_caller_identity" "current" {}
data "aws_region" "current" {}
data "aws_availability_zones" "available" {
  state = "available"
}

locals {
  account_id  = data.aws_caller_identity.current.account_id
  region      = data.aws_region.current.name
  name_prefix = "${var.resource_prefix}-${var.environment}"

  azs = slice(data.aws_availability_zones.available.names, 0, var.availability_zone_count)

  use_tls     = var.domain_name != "" && var.route53_zone_id != ""
  runtime_image = var.runtime_image != "" ? var.runtime_image : "${var.ecr_repository_urls["agent-factory-runtime"]}:latest"
  console_image = var.console_image != "" ? var.console_image : "${var.ecr_repository_urls["agent-factory-console"]}:latest"
}
