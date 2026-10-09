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

output "model_host_asg" {
  value = local.host_tier ? aws_autoscaling_group.model_host[0].name : null
}

output "dashboard_url" {
  value = "https://${var.region}.console.aws.amazon.com/cloudwatch/home?region=${var.region}#dashboards/dashboard/${aws_cloudwatch_dashboard.main.dashboard_name}"
}
