# Secrets Manager holds every credential the API needs. Terraform creates the
# database DSN (it generated that password) and only the *containers* of the
# operator-supplied secrets: their values are put with `aws secretsmanager
# put-secret-value` (README), so a GitHub App key never enters Terraform state.
# All are encrypted with the module's KMS key.

resource "aws_secretsmanager_secret" "database_dsn" {
  name                    = "${local.name}/database-dsn"
  description             = "SWARM_STORAGE_POSTGRES_DSN (generated)"
  kms_key_id              = aws_kms_key.main.arn
  recovery_window_in_days = var.protect_data ? 30 : 0
}

resource "aws_secretsmanager_secret_version" "database_dsn" {
  secret_id = aws_secretsmanager_secret.database_dsn.id
  # sslmode=require matches rds.force_ssl.
  secret_string = "postgresql://${aws_db_instance.main.username}:${random_password.db.result}@${aws_db_instance.main.address}:${aws_db_instance.main.port}/${aws_db_instance.main.db_name}?sslmode=require"
}

locals {
  # name => what to put in it. Plain strings unless noted.
  operator_secrets = {
    "github-client-secret"   = "SWARM_WEB_GITHUB_CLIENT_SECRET: the GitHub App's OAuth client secret"
    "github-webhook-secret"  = "SWARM_WEB_GITHUB_WEBHOOK_SECRET: the App's webhook secret"
    "github-app-private-key" = "SWARM_WEB_GITHUB_APP_PRIVATE_KEY: the App's PEM private key (real newlines or literal \\n)"
    "local-key"              = "SWARM_WEB_LOCAL_KEY: base64 of 32 random bytes (openssl rand -base64 32); seals tenants' provider keys until the KMS key wrapper exists"
    "storage-s3-credentials" = "JSON {\"access_key_id\": \"...\", \"secret_access_key\": \"...\"} for the ${local.name}-storage IAM user (README)"
  }
}

resource "aws_secretsmanager_secret" "operator" {
  for_each                = local.operator_secrets
  name                    = "${local.name}/${each.key}"
  description             = each.value
  kms_key_id              = aws_kms_key.main.arn
  recovery_window_in_days = var.protect_data ? 30 : 0
}
