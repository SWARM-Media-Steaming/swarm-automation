output "alb_dns_name" {
  description = "Point a DNS record for domain_name at this."
  value       = aws_lb.main.dns_name
}

output "public_url" {
  description = "SWARM_WEB_PUBLIC_URL; also the GitHub App's callback origin."
  value       = local.public_url
}

output "github_callback_url" {
  value = "${local.public_url}/api/v1/auth/github/callback"
}

output "github_webhook_url" {
  value = "${local.public_url}/api/v1/webhooks/github"
}

output "ecr_api_repository" {
  value = aws_ecr_repository.api.repository_url
}

output "ecr_worker_repository" {
  value = aws_ecr_repository.worker.repository_url
}

output "bucket" {
  value = aws_s3_bucket.data.id
}

output "kms_key_arn" {
  value = aws_kms_key.main.arn
}

output "cluster" {
  value = aws_ecs_cluster.main.name
}

output "storage_user" {
  description = "Mint its access key by hand (README) and store it in the storage-s3-credentials secret."
  value       = aws_iam_user.storage.name
}

output "operator_secret_ids" {
  description = "Secrets Manager secrets to populate before api_desired_count = 1."
  value       = { for name, secret in aws_secretsmanager_secret.operator : name => secret.name }
}
