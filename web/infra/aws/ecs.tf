resource "aws_ecs_cluster" "main" {
  name = local.name

  setting {
    name  = "containerInsights"
    value = "enabled"
  }
}

resource "aws_cloudwatch_log_group" "api" {
  name              = "/${local.name}/api"
  retention_in_days = var.log_retention_days
  kms_key_id        = aws_kms_key.main.arn
}

resource "aws_cloudwatch_log_group" "jobs" {
  name              = "/${local.name}/jobs"
  retention_in_days = var.log_retention_days
  kms_key_id        = aws_kms_key.main.arn
}

locals {
  secret_arns = { for name, secret in aws_secretsmanager_secret.operator : name => secret.arn }

  # The API's environment. No KMS key id is passed on purpose: this build
  # refuses a KMS configuration it cannot honor.
  api_environment = {
    SWARM_WEB_BIND                = "0.0.0.0:8080"
    SWARM_WEB_STORE               = "postgres"
    SWARM_WEB_PUBLIC_URL          = local.public_url
    SWARM_WEB_GITHUB_CLIENT_ID    = var.github_client_id
    SWARM_WEB_GITHUB_APP_ID       = tostring(var.github_app_id)
    SWARM_WEB_GITHUB_APP_SLUG     = var.github_app_slug
    SWARM_WEB_TRUSTED_AUTHORS     = join(",", var.trusted_authors)
    SWARM_WEB_BRIDGE              = "python"
    SWARM_WEB_JOB_RUNNER          = "fargate"
    SWARM_WEB_JOB_PROVIDER        = var.job_provider
    SWARM_WEB_WORKER_IMAGE        = var.worker_image
    SWARM_WEB_ECS_REGION          = var.region
    SWARM_WEB_ECS_CLUSTER         = aws_ecs_cluster.main.name
    SWARM_WEB_ECS_TASK_DEFINITION = local.job_family
    SWARM_WEB_ECS_SUBNETS         = join(",", aws_subnet.private[*].id)
    SWARM_WEB_ECS_SECURITY_GROUPS = aws_security_group.job.id
    # Fargate CPU units, not millicores (1024 = 1 vCPU).
    SWARM_WEB_JOB_CPU_MILLIS      = tostring(var.job_cpu)
    SWARM_WEB_JOB_MEMORY_MIB      = tostring(var.job_memory_mib)
    SWARM_JOB_STORAGE             = "hosted"
    SWARM_STORAGE_S3_ENDPOINT     = "https://s3.${var.region}.amazonaws.com"
    SWARM_STORAGE_S3_BUCKET       = aws_s3_bucket.data.id
    SWARM_STORAGE_S3_REGION       = var.region
    SWARM_STORAGE_S3_ADDRESSING   = "virtual"
    SWARM_STORAGE_POSTGRES_DRIVER = "psycopg:connect"
  }

  api_secrets = {
    SWARM_WEB_GITHUB_CLIENT_SECRET     = local.secret_arns["github-client-secret"]
    SWARM_WEB_GITHUB_WEBHOOK_SECRET    = local.secret_arns["github-webhook-secret"]
    SWARM_WEB_GITHUB_APP_PRIVATE_KEY   = local.secret_arns["github-app-private-key"]
    SWARM_WEB_LOCAL_KEY                = local.secret_arns["local-key"]
    SWARM_STORAGE_POSTGRES_DSN         = aws_secretsmanager_secret.database_dsn.arn
    SWARM_STORAGE_S3_ACCESS_KEY_ID     = "${local.secret_arns["storage-s3-credentials"]}:access_key_id::"
    SWARM_STORAGE_S3_SECRET_ACCESS_KEY = "${local.secret_arns["storage-s3-credentials"]}:secret_access_key::"
  }
}

resource "aws_ecs_task_definition" "api" {
  family                   = "${local.name}-api"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = var.api_cpu
  memory                   = var.api_memory_mib
  execution_role_arn       = aws_iam_role.api_execution.arn
  task_role_arn            = aws_iam_role.api_task.arn

  runtime_platform {
    operating_system_family = "LINUX"
    cpu_architecture        = "X86_64"
  }

  # The root filesystem is read-only; /tmp is the one writable path (the
  # bridge's scratch files).
  volume {
    name = "tmp"
  }

  container_definitions = jsonencode([{
    name                   = "api"
    image                  = var.api_image
    essential              = true
    readonlyRootFilesystem = true
    user                   = "10001:10001"
    portMappings           = [{ containerPort = 8080, protocol = "tcp" }]
    environment            = [for name, value in local.api_environment : { name = name, value = value }]
    secrets                = [for name, arn in local.api_secrets : { name = name, valueFrom = arn }]
    mountPoints            = [{ sourceVolume = "tmp", containerPath = "/tmp", readOnly = false }]
    linuxParameters        = { initProcessEnabled = true, capabilities = { drop = ["ALL"] } }
    healthCheck = {
      command     = ["CMD", "python3", "-I", "-c", "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/api/v1/health', timeout=4).status == 200 else 1)"]
      interval    = 15
      timeout     = 5
      retries     = 5
      startPeriod = 20
    }
    logConfiguration = {
      logDriver = "awslogs"
      options = {
        awslogs-group         = aws_cloudwatch_log_group.api.name
        awslogs-region        = var.region
        awslogs-stream-prefix = "api"
      }
    }
  }])
}

# The job definition. The API starts it with RunTask and overrides the command,
# the environment and CPU/memory per job; everything that must not vary is
# fixed here: image, user, read-only root, ephemeral volumes, the empty task
# role, the log group. The container is named "worker" because the runner
# overrides it by that name.
resource "aws_ecs_task_definition" "job" {
  family                   = local.job_family
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = var.job_cpu
  memory                   = var.job_memory_mib
  execution_role_arn       = aws_iam_role.job_execution.arn
  task_role_arn            = aws_iam_role.job_task.arn

  ephemeral_storage {
    size_in_gib = var.job_ephemeral_storage_gib
  }

  runtime_platform {
    operating_system_family = "LINUX"
    cpu_architecture        = "X86_64"
  }

  volume {
    name = "workspace"
  }
  volume {
    name = "home"
  }
  volume {
    name = "tmp"
  }

  container_definitions = jsonencode([{
    name                   = "worker"
    image                  = var.worker_image
    essential              = true
    readonlyRootFilesystem = true
    user                   = "1000:1000"
    environment = [
      { name = "AWS_EC2_METADATA_DISABLED", value = "true" },
      { name = "HOME", value = "/home/swarm" },
    ]
    mountPoints = [
      { sourceVolume = "workspace", containerPath = "/workspace", readOnly = false },
      { sourceVolume = "home", containerPath = "/home/swarm", readOnly = false },
      { sourceVolume = "tmp", containerPath = "/tmp", readOnly = false },
    ]
    linuxParameters = { initProcessEnabled = true, capabilities = { drop = ["ALL"] } }
    logConfiguration = {
      logDriver = "awslogs"
      options = {
        awslogs-group         = aws_cloudwatch_log_group.jobs.name
        awslogs-region        = var.region
        awslogs-stream-prefix = "job"
      }
    }
  }])
}

resource "aws_ecs_service" "api" {
  name            = "${local.name}-api"
  cluster         = aws_ecs_cluster.main.id
  task_definition = aws_ecs_task_definition.api.arn
  desired_count   = var.api_desired_count
  launch_type     = "FARGATE"

  # One task: sessions live in memory. A deploy stops it first.
  deployment_minimum_healthy_percent = 0
  deployment_maximum_percent         = 100
  health_check_grace_period_seconds  = 60
  enable_execute_command             = false

  deployment_circuit_breaker {
    enable   = true
    rollback = true
  }

  network_configuration {
    subnets          = aws_subnet.private[*].id
    security_groups  = [aws_security_group.api.id]
    assign_public_ip = false
  }

  load_balancer {
    target_group_arn = aws_lb_target_group.api.arn
    container_name   = "api"
    container_port   = 8080
  }

  depends_on = [aws_lb_listener.https, aws_secretsmanager_secret_version.database_dsn]
}
