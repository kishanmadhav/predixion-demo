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
