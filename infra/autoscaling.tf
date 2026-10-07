resource "aws_appautoscaling_target" "app" {
  service_namespace  = "ecs"
  resource_id        = "service/${aws_ecs_cluster.main.name}/${aws_ecs_service.app.name}"
  scalable_dimension = "ecs:service:DesiredCount"
  min_capacity       = var.min_tasks
  max_capacity       = var.max_tasks
}

# Scale on what a voice task actually runs out of: concurrent calls, not CPU.
# ActiveCalls is emitted per task with only the ServiceName dimension, so its Average
# is "calls per task": double the tasks, halve the value.
resource "aws_appautoscaling_policy" "active_calls" {
  name               = "active-calls-per-task"
  policy_type        = "TargetTrackingScaling"
  service_namespace  = aws_appautoscaling_target.app.service_namespace
  resource_id        = aws_appautoscaling_target.app.resource_id
  scalable_dimension = aws_appautoscaling_target.app.scalable_dimension

  target_tracking_scaling_policy_configuration {
    target_value       = var.target_active_calls
    scale_out_cooldown = 60
    scale_in_cooldown  = 180
    customized_metric_specification {
      metric_name = "ActiveCalls"
      namespace   = local.metric_namespace
      statistic   = "Average"
      dimensions {
        name  = "ServiceName"
        value = local.service_name
      }
    }
  }
}

# Campaign windows are known in advance: pre-warm instead of waiting for the ramp.
resource "aws_appautoscaling_scheduled_action" "campaign_start" {
  count              = var.campaign_prewarm == null ? 0 : 1
  name               = "campaign-prewarm"
  service_namespace  = aws_appautoscaling_target.app.service_namespace
  resource_id        = aws_appautoscaling_target.app.resource_id
  scalable_dimension = aws_appautoscaling_target.app.scalable_dimension
  schedule           = var.campaign_prewarm.start
  scalable_target_action {
    min_capacity = var.campaign_prewarm.min_tasks
    max_capacity = var.max_tasks
  }
}

resource "aws_appautoscaling_scheduled_action" "campaign_end" {
  count              = var.campaign_prewarm == null ? 0 : 1
  name               = "campaign-end"
  service_namespace  = aws_appautoscaling_target.app.service_namespace
  resource_id        = aws_appautoscaling_target.app.resource_id
  scalable_dimension = aws_appautoscaling_target.app.scalable_dimension
  schedule           = var.campaign_prewarm.end
  scalable_target_action {
    min_capacity = var.min_tasks
    max_capacity = var.max_tasks
  }
  depends_on = [aws_appautoscaling_scheduled_action.campaign_start]
}
