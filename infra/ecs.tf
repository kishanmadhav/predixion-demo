resource "aws_security_group" "tasks" {
  name        = "${local.name}-tasks"
  description = "voice-agent tasks: 8080 from the ALB only"
  vpc_id      = aws_vpc.main.id
  tags        = { Name = "${local.name}-tasks" }
}

resource "aws_vpc_security_group_ingress_rule" "tasks_from_alb" {
  security_group_id            = aws_security_group.tasks.id
  referenced_security_group_id = aws_security_group.alb.id
  ip_protocol                  = "tcp"
  from_port                    = 8080
  to_port                      = 8080
}

resource "aws_vpc_security_group_egress_rule" "tasks_out" {
  security_group_id = aws_security_group.tasks.id
  cidr_ipv4         = "0.0.0.0/0" # NLB (model tier), DynamoDB endpoint, ECR via NAT
  ip_protocol       = "-1"
}

# Container Insights creates this group itself, outside Terraform, and it would survive
# destroy. Declaring it here makes Terraform own and delete it.
resource "aws_cloudwatch_log_group" "container_insights" {
  name              = "/aws/ecs/containerinsights/${local.name}/performance"
  retention_in_days = var.log_retention_days
}

resource "aws_ecs_cluster" "main" {
  name       = local.name
  depends_on = [aws_cloudwatch_log_group.container_insights]
  setting {
    name  = "containerInsights"
    value = "enabled"
  }
}

locals {
  provider_kind = var.model_tier == "mock" ? "mock" : "openai"
  default_stage_timeouts = {
    mock = { stt = 5, llm = 8, tts = 5 }
    gpu  = { stt = 5, llm = 8, tts = 5 }
    cpu  = { stt = 8, llm = 15, tts = 12 } # CPU models are slower; TTS queues first (measured 2026-10-09)
  }
  stage_timeouts  = coalesce(var.stage_timeouts_s, local.default_stage_timeouts[var.model_tier])
  turn_deadline_s = coalesce(var.turn_deadline_s, var.model_tier == "cpu" ? 35 : 20)
  app_env = {
    HOST             = "0.0.0.0"
    PORT             = "8080"
    LOG_LEVEL        = "INFO"
    STORE_BACKEND    = "dynamodb"
    DYNAMODB_TABLE   = aws_dynamodb_table.state.name
    AWS_REGION       = var.region
    EMF_ENABLED      = "true"
    EMF_NAMESPACE    = local.metric_namespace
    EMF_SERVICE_NAME = local.service_name
    TURN_DEADLINE_S  = tostring(local.turn_deadline_s)
    STT_PROVIDER     = local.provider_kind
    STT_BASE_URL     = local.provider_url
    STT_MODEL        = local.stt_model
    STT_TIMEOUT_S    = tostring(local.stage_timeouts.stt)
    LLM_PROVIDER     = local.provider_kind
    LLM_BASE_URL     = local.provider_url
    LLM_MODEL        = local.llm_model
    LLM_TIMEOUT_S    = tostring(local.stage_timeouts.llm)
    LLM_MAX_TOKENS   = var.model_tier == "cpu" ? "40" : "80" # short replies: a voice turn is a sentence or two, and fewer tokens means lower latency
    TTS_PROVIDER     = local.provider_kind
    TTS_BASE_URL     = local.provider_url
    TTS_MODEL        = var.tts_model
    TTS_VOICE        = var.tts_voice
    TTS_TIMEOUT_S    = tostring(local.stage_timeouts.tts)
  }
}

resource "aws_ecs_task_definition" "app" {
  family                   = local.service_name
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = var.task_cpu
  memory                   = var.task_memory
  execution_role_arn       = aws_iam_role.execution.arn
  task_role_arn            = aws_iam_role.task.arn
  runtime_platform {
    operating_system_family = "LINUX"
    cpu_architecture        = "X86_64"
  }
  container_definitions = jsonencode([{
    name         = local.service_name
    image        = local.app_image
    essential    = true
    command      = ["voice-agent", "serve"]
    portMappings = [{ containerPort = 8080, protocol = "tcp" }]
    environment  = [for k, v in local.app_env : { name = k, value = v }]
    stopTimeout  = 30
    logConfiguration = {
      logDriver = "awslogs"
      options = {
        awslogs-group         = aws_cloudwatch_log_group.app.name
        awslogs-region        = var.region
        awslogs-stream-prefix = local.service_name
      }
    }
  }])
}

resource "aws_ecs_service" "app" {
  name                               = local.service_name
  cluster                            = aws_ecs_cluster.main.id
  task_definition                    = aws_ecs_task_definition.app.arn
  desired_count                      = var.min_tasks
  launch_type                        = "FARGATE"
  health_check_grace_period_seconds  = 30
  deployment_minimum_healthy_percent = 100
  deployment_maximum_percent         = 200
  availability_zone_rebalancing      = "ENABLED"
  propagate_tags                     = "SERVICE"
  wait_for_steady_state              = true

  network_configuration {
    subnets          = aws_subnet.private[*].id
    security_groups  = [aws_security_group.tasks.id]
    assign_public_ip = false
  }

  load_balancer {
    target_group_arn = aws_lb_target_group.app.arn
    container_name   = local.service_name
    container_port   = 8080
  }

  deployment_circuit_breaker {
    enable   = true
    rollback = true
  }

  lifecycle {
    ignore_changes = [desired_count] # owned by autoscaling after creation
  }

  depends_on = [aws_lb_listener.http]
}
