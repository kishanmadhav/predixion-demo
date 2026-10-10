resource "aws_cloudwatch_log_group" "app" {
  name              = "/ecs/${local.name}"
  retention_in_days = var.log_retention_days
}

resource "aws_cloudwatch_log_group" "model_host" {
  name              = "/models/${local.name}"
  retention_in_days = var.log_retention_days
}
