output "state_table" {
  value = aws_dynamodb_table.state.name
}

output "app_image" {
  value = local.app_image
}
