terraform {
  required_version = ">= 1.6"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.80"
    }
    random = {
      source  = "hashicorp/random"
      version = "~> 3.6"
    }
  }

  # State holds the generated database password. Configure an encrypted,
  # access-controlled remote backend before the first apply, for example:
  #
  #   terraform init \
  #     -backend-config="bucket=<state bucket>" \
  #     -backend-config="key=swarm-web/terraform.tfstate" \
  #     -backend-config="region=<region>" \
  #     -backend-config="encrypt=true" \
  #     -backend-config="use_lockfile=true"
  #
  # CI runs `init -backend=false`, so no backend is declared here.
}

provider "aws" {
  region = var.region

  default_tags {
    tags = {
      Project   = "swarm-automation"
      Component = "web"
      ManagedBy = "terraform"
    }
  }
}

data "aws_caller_identity" "current" {}
data "aws_partition" "current" {}
data "aws_availability_zones" "available" {
  state = "available"
}

locals {
  name       = var.name_prefix
  account_id = data.aws_caller_identity.current.account_id
  partition  = data.aws_partition.current.partition
  azs        = slice(data.aws_availability_zones.available.names, 0, 2)
  bucket     = coalesce(var.bucket_name, "${var.name_prefix}-${local.account_id}-${var.region}")
  public_url = "https://${var.domain_name}"

  # The job task definition's family. The API addresses it as
  # SWARM_WEB_ECS_TASK_DEFINITION; the container inside is always "worker"
  # (the runner overrides that container by name).
  job_family = "${local.name}-worker"
}
