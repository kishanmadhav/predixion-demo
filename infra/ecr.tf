resource "aws_ecr_repository" "app" {
  name                 = local.name
  image_tag_mutability = "IMMUTABLE"
  force_delete         = true # `terraform destroy` must leave nothing behind
  image_scanning_configuration {
    scan_on_push = true
  }
}

resource "aws_ecr_lifecycle_policy" "app" {
  repository = aws_ecr_repository.app.name
  policy = jsonencode({
    rules = [{
      rulePriority = 1
      description  = "keep the last 5 images"
      selection    = { tagStatus = "any", countType = "imageCountMoreThan", countNumber = 5 }
      action       = { type = "expire" }
    }]
  })
}

locals {
  app_root  = abspath("${path.module}/..")
  src_files = sort(concat(tolist(fileset(local.app_root, "src/**/*.py")), ["pyproject.toml", "uv.lock", "Dockerfile"]))
  src_hash  = substr(sha1(join("", [for f in local.src_files : filesha1("${local.app_root}/${f}")])), 0, 12)
}

resource "docker_image" "app" {
  name = "${aws_ecr_repository.app.repository_url}:${local.src_hash}"
  build {
    context  = local.app_root
    platform = "linux/amd64"
  }
  triggers     = { src = local.src_hash }
  keep_locally = true
}

resource "docker_registry_image" "app" {
  name          = docker_image.app.name
  keep_remotely = true
  triggers      = { src = local.src_hash }
}

locals {
  # Pinned by digest: what was tested is exactly what runs.
  app_image = "${aws_ecr_repository.app.repository_url}@${docker_registry_image.app.sha256_digest}"
}
