locals {
  svc          = ["ServiceName", local.service_name]
  ns           = local.metric_namespace
  alb          = aws_lb.app.arn_suffix
  tg           = aws_lb_target_group.app.arn_suffix
  ecsd         = ["ClusterName", aws_ecs_cluster.main.name, "ServiceName", aws_ecs_service.app.name]
  stage_metric = { for s in ["stt", "llm", "tts"] : s => ["ServiceName", local.service_name, "Stage", s] }
}

# ---- ECS task / service events -> logs (autoscaling activity on the dashboard) -------
resource "aws_cloudwatch_log_group" "events" {
  name              = "/aws/events/${local.name}"
  retention_in_days = var.log_retention_days
}

data "aws_iam_policy_document" "events_to_logs" {
  statement {
    actions   = ["logs:CreateLogStream", "logs:PutLogEvents"]
    resources = ["${aws_cloudwatch_log_group.events.arn}:*"]
    principals {
      type        = "Service"
      identifiers = ["events.amazonaws.com", "delivery.logs.amazonaws.com"]
    }
  }
}

resource "aws_cloudwatch_log_resource_policy" "events" {
  policy_name     = "${local.name}-events"
  policy_document = data.aws_iam_policy_document.events_to_logs.json
}

resource "aws_cloudwatch_event_rule" "ecs" {
  name = "${local.name}-ecs-events"
  # Deployment events carry no detail.clusterArn, so they match on the service ARN in resources.
  event_pattern = jsonencode({
    source = ["aws.ecs"]
    "$or" = [
      {
        detail-type = ["ECS Task State Change", "ECS Service Action"]
        detail      = { clusterArn = [aws_ecs_cluster.main.arn] }
      },
      {
        detail-type = ["ECS Deployment State Change"]
        resources   = [{ prefix = aws_ecs_service.app.id }]
      },
    ]
  })
}

resource "aws_cloudwatch_event_target" "ecs_to_logs" {
  rule = aws_cloudwatch_event_rule.ecs.name
  arn  = aws_cloudwatch_log_group.events.arn
}

# The target-tracking alarms Application Auto Scaling manages: the real scale-out/in decisions.
resource "aws_cloudwatch_event_rule" "scaling_alarms" {
  name = "${local.name}-scaling-alarms"
  event_pattern = jsonencode({
    source      = ["aws.cloudwatch"]
    detail-type = ["CloudWatch Alarm State Change"]
    detail      = { alarmName = [{ prefix = "TargetTracking-service/${aws_ecs_cluster.main.name}/${local.service_name}" }] }
  })
}

resource "aws_cloudwatch_event_target" "scaling_alarms_to_logs" {
  rule = aws_cloudwatch_event_rule.scaling_alarms.name
  arn  = aws_cloudwatch_log_group.events.arn
}

# ---- Alarms ---------------------------------------------------------------------------
resource "aws_sns_topic" "alarms" {
  name = "${local.name}-alarms"
}

resource "aws_sns_topic_subscription" "email" {
  count     = var.alert_email == null ? 0 : 1
  topic_arn = aws_sns_topic.alarms.arn
  protocol  = "email"
  endpoint  = var.alert_email
}

resource "aws_cloudwatch_metric_alarm" "p99_latency" {
  alarm_name          = "${local.name}-p99-latency-3x-baseline"
  alarm_description   = "RUNBOOK trigger: p99 turn latency > 3x baseline. See RUNBOOK.md."
  namespace           = local.ns
  metric_name         = "TurnLatency"
  dimensions          = { ServiceName = local.service_name }
  extended_statistic  = "p99"
  period              = 60
  evaluation_periods  = 3
  datapoints_to_alarm = 3
  comparison_operator = "GreaterThanThreshold"
  threshold           = 3 * var.baseline_p99_ms
  treat_missing_data  = "notBreaching"
  alarm_actions       = [aws_sns_topic.alarms.arn]
  ok_actions          = [aws_sns_topic.alarms.arn]
}

resource "aws_cloudwatch_metric_alarm" "target_5xx" {
  alarm_name          = "${local.name}-http-5xx"
  alarm_description   = "The service itself returned 5xx (degraded turns are 200s; this should stay 0)."
  namespace           = "AWS/ApplicationELB"
  metric_name         = "HTTPCode_Target_5XX_Count"
  dimensions          = { LoadBalancer = local.alb, TargetGroup = local.tg }
  statistic           = "Sum"
  period              = 60
  evaluation_periods  = 2
  comparison_operator = "GreaterThanThreshold"
  threshold           = 5
  treat_missing_data  = "notBreaching"
  alarm_actions       = [aws_sns_topic.alarms.arn]
}

resource "aws_cloudwatch_metric_alarm" "breaker_open" {
  for_each            = local.stage_metric
  alarm_name          = "${local.name}-breaker-open-${each.key}"
  alarm_description   = "The ${each.key} circuit breaker is open on at least one task: dependency outage."
  namespace           = local.ns
  metric_name         = "BreakerState"
  dimensions          = { ServiceName = local.service_name, Stage = each.key }
  statistic           = "Maximum"
  period              = 60
  evaluation_periods  = 1
  comparison_operator = "GreaterThanOrEqualToThreshold"
  threshold           = 2
  treat_missing_data  = "notBreaching"
  alarm_actions       = [aws_sns_topic.alarms.arn]
  ok_actions          = [aws_sns_topic.alarms.arn]
}

resource "aws_cloudwatch_metric_alarm" "dlq_pending" {
  alarm_name          = "${local.name}-dlq-pending"
  alarm_description   = "Dead letters are piling up: replay once the dependency recovers (RUNBOOK.md)."
  namespace           = local.ns
  metric_name         = "DlqPending"
  dimensions          = { ServiceName = local.service_name }
  statistic           = "Maximum"
  period              = 60
  evaluation_periods  = 5
  comparison_operator = "GreaterThanThreshold"
  threshold           = 50
  treat_missing_data  = "notBreaching"
  alarm_actions       = [aws_sns_topic.alarms.arn]
}

# ---- Dashboard ------------------------------------------------------------------------
locals {
  widgets = [
    {
      type       = "text", x = 0, y = 0, width = 24, height = 2
      properties = { markdown = "## Collections voice-agent (ap-south-1)\nTraffic, capacity, dependencies. Runbook trigger: p99 turn latency > 3x baseline (${3 * var.baseline_p99_ms} ms). Degraded turns are answered with a fallback prompt and dead-lettered; the replay path is in RUNBOOK.md." }
    },
    {
      type = "metric", x = 0, y = 2, width = 8, height = 6
      properties = {
        title = "Request rate (turns/min)", region = var.region, view = "timeSeries", stat = "Sum", period = 60
        metrics = [
          [local.ns, "Turns", local.svc[0], local.svc[1], { label = "turns" }],
          ["AWS/ApplicationELB", "RequestCount", "LoadBalancer", local.alb, { label = "ALB requests" }],
        ]
      }
    },
    {
      type = "metric", x = 8, y = 2, width = 8, height = 6
      properties = {
        title = "Error rate (%)", region = var.region, view = "timeSeries", period = 60, yAxis = { left = { min = 0 } }
        metrics = [
          [{ expression = "100 * IF(m1 > 0, m2 / m1, 0)", label = "degraded turns %", id = "e1" }],
          [{ expression = "100 * IF(m3 > 0, m4 / m3, 0)", label = "HTTP 5xx %", id = "e2" }],
          [local.ns, "Turns", local.svc[0], local.svc[1], { id = "m1", stat = "Sum", visible = false }],
          [local.ns, "TurnsDegraded", local.svc[0], local.svc[1], { id = "m2", stat = "Sum", visible = false }],
          ["AWS/ApplicationELB", "RequestCount", "LoadBalancer", local.alb, { id = "m3", stat = "Sum", visible = false }],
          ["AWS/ApplicationELB", "HTTPCode_Target_5XX_Count", "LoadBalancer", local.alb, { id = "m4", stat = "Sum", visible = false }],
        ]
      }
    },
    {
      type = "metric", x = 16, y = 2, width = 8, height = 6
      properties = {
        title = "Turn latency percentiles (ms)", region = var.region, view = "timeSeries", period = 60
        metrics = [
          [local.ns, "TurnLatency", local.svc[0], local.svc[1], { stat = "p50", label = "p50" }],
          [local.ns, "TurnLatency", local.svc[0], local.svc[1], { stat = "p90", label = "p90" }],
          [local.ns, "TurnLatency", local.svc[0], local.svc[1], { stat = "p99", label = "p99" }],
        ]
        annotations = { horizontal = [{ label = "3x baseline p99", value = 3 * var.baseline_p99_ms }] }
      }
    },
    {
      type = "metric", x = 0, y = 8, width = 8, height = 6
      properties = {
        title = "Active calls (scaling signal)", region = var.region, view = "timeSeries", period = 60
        metrics = [
          [local.ns, "ActiveCalls", local.svc[0], local.svc[1], { stat = "Average", label = "per task (target ${var.target_active_calls})" }],
          [local.ns, "ActiveCalls", local.svc[0], local.svc[1], { stat = "Sum", label = "sum of samples", visible = false }],
          [local.ns, "InFlightTurns", local.svc[0], local.svc[1], { stat = "Average", label = "in-flight turns per task" }],
        ]
        annotations = { horizontal = [{ label = "target", value = var.target_active_calls }] }
      }
    },
    {
      type = "metric", x = 8, y = 8, width = 8, height = 6
      properties = {
        title = "Tasks: desired vs running (autoscaling)", region = var.region, view = "timeSeries", stat = "Average", period = 60
        metrics = [
          ["ECS/ContainerInsights", "DesiredTaskCount", local.ecsd[0], local.ecsd[1], local.ecsd[2], local.ecsd[3], { label = "desired" }],
          ["ECS/ContainerInsights", "RunningTaskCount", local.ecsd[0], local.ecsd[1], local.ecsd[2], local.ecsd[3], { label = "running" }],
        ]
      }
    },
    {
      type = "metric", x = 16, y = 8, width = 8, height = 6
      properties = {
        title = "Task CPU / memory (%)", region = var.region, view = "timeSeries", stat = "Average", period = 60
        metrics = [
          ["AWS/ECS", "CPUUtilization", local.ecsd[0], local.ecsd[1], local.ecsd[2], local.ecsd[3]],
          ["AWS/ECS", "MemoryUtilization", local.ecsd[0], local.ecsd[1], local.ecsd[2], local.ecsd[3]],
        ]
      }
    },
    {
      type = "metric", x = 0, y = 14, width = 8, height = 6
      properties = {
        title = "Provider failures by stage (attempts)", region = var.region, view = "timeSeries", stat = "Sum", period = 60
        metrics = concat(
          [for s, d in local.stage_metric : [local.ns, "ProviderFailures", d[0], d[1], d[2], d[3], { label = "${s} failures" }]],
          [for s, d in local.stage_metric : [local.ns, "Retries", d[0], d[1], d[2], d[3], { label = "${s} retries" }]],
        )
      }
    },
    {
      type = "metric", x = 8, y = 14, width = 8, height = 6
      properties = {
        title   = "Circuit breakers (0 closed, 1 half-open, 2 open)", region = var.region, view = "timeSeries", stat = "Maximum", period = 60
        yAxis   = { left = { min = 0, max = 2 } }
        metrics = [for s, d in local.stage_metric : [local.ns, "BreakerState", d[0], d[1], d[2], d[3], { label = s }]]
      }
    },
    {
      type = "metric", x = 16, y = 14, width = 8, height = 6
      properties = {
        title = "Dead-letter queue", region = var.region, view = "timeSeries", period = 60
        metrics = [
          [local.ns, "DlqPending", local.svc[0], local.svc[1], { stat = "Maximum", label = "pending" }],
          [local.ns, "DeadLetters", local.svc[0], local.svc[1], { stat = "Sum", label = "new per min" }],
          [local.ns, "ReplaysResolved", local.svc[0], local.svc[1], { stat = "Sum", label = "resolved by replay" }],
        ]
      }
    },
    {
      type = "metric", x = 0, y = 20, width = 8, height = 6
      properties = {
        title   = "Model tier health (NLB healthy hosts)", region = var.region, view = "timeSeries", stat = "Minimum", period = 60
        metrics = [["AWS/NetworkELB", "HealthyHostCount", "TargetGroup", aws_lb_target_group.models.arn_suffix, "LoadBalancer", aws_lb.models.arn_suffix]]
      }
    },
    {
      type = "log", x = 8, y = 20, width = 16, height = 6
      properties = {
        title  = "Autoscaling and task events"
        region = var.region
        view   = "table"
        query  = "SOURCE '${aws_cloudwatch_log_group.events.name}' | fields @timestamp, `detail-type`, detail.group, detail.lastStatus, detail.eventName, detail.alarmName, detail.state.value | sort @timestamp desc | limit 50"
      }
    },
  ]
}

resource "aws_cloudwatch_dashboard" "main" {
  dashboard_name = local.name
  dashboard_body = jsonencode({ widgets = local.widgets })
}
