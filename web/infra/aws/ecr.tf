# Image repositories. Building and pushing is documented, not automated
# (docs/web-architecture.md, "Building and publishing images").

resource "aws_ecr_repository" "api" {
  name                 = "${local.name}/api"
  image_tag_mutability = "IMMUTABLE"
  force_delete         = !var.protect_data

  image_scanning_configuration { scan_on_push = true }
  encryption_configuration {
    encryption_type = "KMS"
    kms_key         = aws_kms_key.main.arn
  }
}

resource "aws_ecr_repository" "worker" {
  name                 = "${local.name}/worker"
  image_tag_mutability = "IMMUTABLE"
  force_delete         = !var.protect_data

  image_scanning_configuration { scan_on_push = true }
  encryption_configuration {
    encryption_type = "KMS"
    kms_key         = aws_kms_key.main.arn
  }
}

resource "aws_ecr_lifecycle_policy" "keep_recent" {
  for_each   = { api = aws_ecr_repository.api.name, worker = aws_ecr_repository.worker.name }
  repository = each.value
  policy = jsonencode({
    rules = [{
      rulePriority = 1
      description  = "Keep the 30 most recent images"
      selection    = { tagStatus = "any", countType = "imageCountMoreThan", countNumber = 30 }
      action       = { type = "expire" }
    }]
  })
}
