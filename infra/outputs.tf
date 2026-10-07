output "state_table" {
  value = aws_dynamodb_table.state.name
}

output "app_image" {
  value = local.app_image
}

output "url" {
  value = "http://${aws_lb.app.dns_name}"
}

output "cluster" {
  value = aws_ecs_cluster.main.name
}

output "models_endpoint" {
  value = local.provider_url
}

output "gpu_asg" {
  value = var.model_tier == "gpu" ? aws_autoscaling_group.gpu[0].name : null
}
