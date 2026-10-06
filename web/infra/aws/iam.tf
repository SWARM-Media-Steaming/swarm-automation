# Least privilege, one role per job of the role:
#
#   execution (API)   ECS agent: pull the API image, write its logs, read the
#                     API's own secrets into the container environment
#   execution (job)   ECS agent: pull the worker image, write job logs. No
#                     secrets: the orchestrator injects a job's environment
#   task (API)        what the running API process may call: RunTask for the
#                     worker task definition on this cluster, stop/describe
#                     those tasks, and pass exactly the two job roles
#   task (job)        NOTHING. A job is untrusted code with a repository
#                     token; it must not inherit an AWS identity. Its storage
#                     access is the scoped storage user below, and the API
#                     never gives it more
#   storage user      S3 objects in the one bucket and the KMS key through S3
#
# Wildcards are limited to what AWS cannot name ahead of time: the task id
# segment of a task ARN and the object key of the bucket.

data "aws_iam_policy_document" "ecs_tasks_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["ecs-tasks.amazonaws.com"]
    }
    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [local.account_id]
    }
  }
}

# --- execution role for the API --------------------------------------------

resource "aws_iam_role" "api_execution" {
  name               = "${local.name}-api-execution"
  assume_role_policy = data.aws_iam_policy_document.ecs_tasks_assume.json
}

data "aws_iam_policy_document" "api_execution" {
  statement {
    sid       = "PullApiImage"
    actions   = ["ecr:BatchGetImage", "ecr:GetDownloadUrlForLayer"]
    resources = [aws_ecr_repository.api.arn]
  }
  statement {
    sid       = "EcrToken"
    actions   = ["ecr:GetAuthorizationToken"]
    resources = ["*"]
  }
  statement {
    sid       = "WriteApiLogs"
    actions   = ["logs:CreateLogStream", "logs:PutLogEvents"]
    resources = ["${aws_cloudwatch_log_group.api.arn}:*"]
  }
  statement {
    sid     = "ReadApiSecrets"
    actions = ["secretsmanager:GetSecretValue"]
    resources = concat(
      [aws_secretsmanager_secret.database_dsn.arn],
      [for secret in aws_secretsmanager_secret.operator : secret.arn],
    )
  }
  statement {
    sid       = "DecryptApiSecrets"
    actions   = ["kms:Decrypt"]
    resources = [aws_kms_key.main.arn]
  }
}

resource "aws_iam_role_policy" "api_execution" {
  name   = "execution"
  role   = aws_iam_role.api_execution.id
  policy = data.aws_iam_policy_document.api_execution.json
}

# --- execution role for jobs -----------------------------------------------

resource "aws_iam_role" "job_execution" {
  name               = "${local.name}-job-execution"
  assume_role_policy = data.aws_iam_policy_document.ecs_tasks_assume.json
}

data "aws_iam_policy_document" "job_execution" {
  statement {
    sid       = "PullWorkerImage"
    actions   = ["ecr:BatchGetImage", "ecr:GetDownloadUrlForLayer"]
    resources = [aws_ecr_repository.worker.arn]
  }
  statement {
    sid       = "EcrToken"
    actions   = ["ecr:GetAuthorizationToken"]
    resources = ["*"]
  }
  statement {
    sid       = "WriteJobLogs"
    actions   = ["logs:CreateLogStream", "logs:PutLogEvents"]
    resources = ["${aws_cloudwatch_log_group.jobs.arn}:*"]
  }
}

resource "aws_iam_role_policy" "job_execution" {
  name   = "execution"
  role   = aws_iam_role.job_execution.id
  policy = data.aws_iam_policy_document.job_execution.json
}

# --- task role for jobs: deliberately empty -----------------------------------

resource "aws_iam_role" "job_task" {
  name               = "${local.name}-job-task"
  description        = "Identity of a worker job. Has no permissions on purpose."
  assume_role_policy = data.aws_iam_policy_document.ecs_tasks_assume.json
}

# Defence in depth: even if someone attaches a managed policy later, a job's
# calls to the services it has no business with stay denied.
data "aws_iam_policy_document" "job_task_deny" {
  statement {
    sid       = "NoAwsControlPlane"
    effect    = "Deny"
    actions   = ["ecs:*", "iam:*", "secretsmanager:*", "kms:*", "ssm:*", "sts:*", "ecr:*"]
    resources = ["*"]
  }
}

resource "aws_iam_role_policy" "job_task_deny" {
  name   = "deny-control-plane"
  role   = aws_iam_role.job_task.id
  policy = data.aws_iam_policy_document.job_task_deny.json
}

# --- task role for the API --------------------------------------------------

resource "aws_iam_role" "api_task" {
  name               = "${local.name}-api-task"
  assume_role_policy = data.aws_iam_policy_document.ecs_tasks_assume.json
}

data "aws_iam_policy_document" "api_task" {
  statement {
    sid       = "StartJobs"
    actions   = ["ecs:RunTask"]
    resources = ["arn:${local.partition}:ecs:${var.region}:${local.account_id}:task-definition/${local.job_family}:*"]
    condition {
      test     = "ArnEquals"
      variable = "ecs:cluster"
      values   = [aws_ecs_cluster.main.arn]
    }
  }
  statement {
    sid       = "TagStartedJobs"
    actions   = ["ecs:TagResource"]
    resources = ["arn:${local.partition}:ecs:${var.region}:${local.account_id}:task/${aws_ecs_cluster.main.name}/*"]
    condition {
      test     = "StringEquals"
      variable = "ecs:CreateAction"
      values   = ["RunTask"]
    }
  }
  statement {
    sid       = "ManageJobs"
    actions   = ["ecs:DescribeTasks", "ecs:StopTask"]
    resources = ["arn:${local.partition}:ecs:${var.region}:${local.account_id}:task/${aws_ecs_cluster.main.name}/*"]
  }
  statement {
    sid       = "PassOnlyTheJobRoles"
    actions   = ["iam:PassRole"]
    resources = [aws_iam_role.job_execution.arn, aws_iam_role.job_task.arn]
    condition {
      test     = "StringEquals"
      variable = "iam:PassedToService"
      values   = ["ecs-tasks.amazonaws.com"]
    }
  }
}

resource "aws_iam_role_policy" "api_task" {
  name   = "orchestrate-jobs"
  role   = aws_iam_role.api_task.id
  policy = data.aws_iam_policy_document.api_task.json
}

# --- storage user ------------------------------------------------------------
# The worker's S3 client (issue_worker/object_store.py) signs with an access key
# from SWARM_STORAGE_S3_*, and a job is not given a role (above), so storage
# access is one narrowly scoped IAM user whose key lives in Secrets Manager and
# reaches jobs only through the API's environment. Terraform does not create
# the access key (it would sit in state); the README mints it.

resource "aws_iam_user" "storage" {
  name = "${local.name}-storage"
}

data "aws_iam_policy_document" "storage" {
  statement {
    sid       = "ListTheBucket"
    actions   = ["s3:ListBucket", "s3:GetBucketLocation"]
    resources = [aws_s3_bucket.data.arn]
  }
  statement {
    sid       = "ObjectsInTheBucket"
    actions   = ["s3:GetObject", "s3:PutObject", "s3:DeleteObject"]
    resources = ["${aws_s3_bucket.data.arn}/*"]
  }
  statement {
    sid       = "KeyThroughS3Only"
    actions   = ["kms:Decrypt", "kms:GenerateDataKey"]
    resources = [aws_kms_key.main.arn]
    condition {
      test     = "StringEquals"
      variable = "kms:ViaService"
      values   = ["s3.${var.region}.amazonaws.com"]
    }
  }
}

resource "aws_iam_user_policy" "storage" {
  name   = "bucket-objects"
  user   = aws_iam_user.storage.name
  policy = data.aws_iam_policy_document.storage.json
}
