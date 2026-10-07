resource "aws_cloudwatch_log_group" "app" {
  name              = "/ecs/${local.name}"
  retention_in_days = var.log_retention_days
}

resource "aws_cloudwatch_log_group" "gpu" {
  name              = "/gpu/${local.name}"
  retention_in_days = var.log_retention_days
}
