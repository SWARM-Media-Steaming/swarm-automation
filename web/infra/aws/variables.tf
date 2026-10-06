variable "region" {
  description = "AWS region for every resource."
  type        = string
  default     = "us-east-1"
}

variable "name_prefix" {
  description = "Prefix for resource names. Lowercase letters, digits and hyphens."
  type        = string
  default     = "swarm"

  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{1,20}$", var.name_prefix))
    error_message = "name_prefix must be 2-21 lowercase letters, digits or hyphens, starting with a letter."
  }
}

variable "domain_name" {
  description = "Public host name users open (for example swarm.example.com). Point a DNS record at the alb_dns_name output; this module does not manage DNS."
  type        = string
}

variable "certificate_arn" {
  description = "ARN of an ACM certificate in this region that covers domain_name. The listener is HTTPS only; port 80 redirects."
  type        = string
}

variable "api_image" {
  description = "Full API image URI with a tag or digest (see web/Dockerfile and docs/web-architecture.md, \"Building and publishing images\")."
  type        = string
}

variable "worker_image" {
  description = "Full worker image URI with a tag or digest (web/worker/Dockerfile)."
  type        = string
}

variable "github_app_id" {
  description = "Numeric id of the GitHub App."
  type        = number
}

variable "github_client_id" {
  description = "The GitHub App's OAuth client id (not a secret)."
  type        = string
}

variable "github_app_slug" {
  description = "The GitHub App's slug, for the install link. Empty hides the link."
  type        = string
  default     = ""
}

variable "trusted_authors" {
  description = "GitHub logins whose comments may steer a job."
  type        = list(string)
  default     = []
}

variable "job_provider" {
  description = "Provider whose key a job receives: claude, codex or grok."
  type        = string
  default     = "claude"

  validation {
    condition     = contains(["claude", "codex", "grok"], var.job_provider)
    error_message = "job_provider must be claude, codex or grok."
  }
}

# --- network ---------------------------------------------------------------

variable "vpc_cidr" {
  description = "CIDR of the new VPC."
  type        = string
  default     = "10.40.0.0/16"
}

variable "alb_ingress_cidrs" {
  description = "Who may reach the load balancer on 443/80. Narrow it for a private beta."
  type        = list(string)
  default     = ["0.0.0.0/0"]
}

# --- database --------------------------------------------------------------

variable "db_instance_class" {
  description = "RDS instance class."
  type        = string
  default     = "db.t4g.medium"
}

variable "db_allocated_storage_gb" {
  description = "Initial RDS storage (storage autoscaling is on)."
  type        = number
  default     = 50
}

variable "db_multi_az" {
  description = "Run a standby in a second zone."
  type        = bool
  default     = true
}

variable "db_backup_retention_days" {
  description = "Automated backup retention."
  type        = number
  default     = 14
}

variable "protect_data" {
  description = "Deletion protection on the database and no force-destroy on the bucket. Turn off only for a throwaway environment."
  type        = bool
  default     = true
}

variable "bucket_name" {
  description = "Name of the checkpoint/log/document bucket. Default: <name_prefix>-<account id>-<region>."
  type        = string
  default     = null
}

# --- compute ---------------------------------------------------------------

variable "api_desired_count" {
  description = "API tasks. Keep 0 until the secrets are populated (README), then 1. The in-memory store is per process, so more than 1 is unsupported until the Postgres store lands."
  type        = number
  default     = 0

  validation {
    condition     = var.api_desired_count >= 0 && var.api_desired_count <= 1
    error_message = "api_desired_count must be 0 or 1 while the API keeps sessions in memory."
  }
}

variable "api_cpu" {
  description = "Fargate CPU units for the API task (1024 = 1 vCPU)."
  type        = number
  default     = 512
}

variable "api_memory_mib" {
  description = "Fargate memory for the API task."
  type        = number
  default     = 1024
}

variable "job_cpu" {
  description = "Fargate CPU units for one job (1024 = 1 vCPU). Sent as SWARM_WEB_JOB_CPU_MILLIS, which the Fargate runner passes to RunTask as CPU units."
  type        = number
  default     = 1024
}

variable "job_memory_mib" {
  description = "Fargate memory for one job. Must be a valid pair with job_cpu."
  type        = number
  default     = 2048
}

variable "job_ephemeral_storage_gib" {
  description = "Ephemeral storage for one job's workspace (21-200)."
  type        = number
  default     = 30
}

variable "log_retention_days" {
  description = "CloudWatch Logs retention for the API and job log groups."
  type        = number
  default     = 30
}

variable "checkpoint_noncurrent_days" {
  description = "Days an overwritten checkpoint/log object version is kept."
  type        = number
  default     = 30
}
