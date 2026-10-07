resource "aws_security_group" "nlb" {
  name        = "${local.name}-models-nlb"
  description = "Internal NLB to the model tier: 8080 from voice-agent tasks only"
  vpc_id      = aws_vpc.main.id
  tags        = { Name = "${local.name}-models-nlb" }
}

resource "aws_vpc_security_group_ingress_rule" "nlb_from_tasks" {
  security_group_id            = aws_security_group.nlb.id
  referenced_security_group_id = aws_security_group.tasks.id
  ip_protocol                  = "tcp"
  from_port                    = 8080
  to_port                      = 8080
}

resource "aws_vpc_security_group_egress_rule" "nlb_to_models" {
  security_group_id            = aws_security_group.nlb.id
  referenced_security_group_id = aws_security_group.models.id
  ip_protocol                  = "tcp"
  from_port                    = 8080
  to_port                      = 8080
}

resource "aws_security_group" "models" {
  name        = "${local.name}-models"
  description = "Model tier (mock task or GPU host): 8080 from the NLB only"
  vpc_id      = aws_vpc.main.id
  tags        = { Name = "${local.name}-models" }
}

resource "aws_vpc_security_group_ingress_rule" "models_from_nlb" {
  security_group_id            = aws_security_group.models.id
  referenced_security_group_id = aws_security_group.nlb.id
  ip_protocol                  = "tcp"
  from_port                    = 8080
  to_port                      = 8080
}

resource "aws_vpc_security_group_egress_rule" "models_out" {
  security_group_id = aws_security_group.models.id
  cidr_ipv4         = "0.0.0.0/0" # image and model downloads via NAT
  ip_protocol       = "-1"
}

resource "aws_lb" "models" {
  name                             = "${local.name}-models"
  load_balancer_type               = "network"
  internal                         = true
  subnets                          = aws_subnet.private[*].id
  security_groups                  = [aws_security_group.nlb.id]
  enable_cross_zone_load_balancing = true # one GPU host serves tasks in both AZs
}

resource "aws_lb_target_group" "models" {
  name                 = "${local.name}-models-${var.model_tier}"
  vpc_id               = aws_vpc.main.id
  target_type          = var.model_tier == "gpu" ? "instance" : "ip"
  port                 = 8080
  protocol             = "TCP"
  deregistration_delay = 10
  preserve_client_ip   = "false" # the models security group admits the NLB security group, not client IPs
  health_check {
    protocol            = "HTTP"
    path                = var.model_tier == "gpu" ? "/ready" : "/health"
    port                = "8080"
    interval            = 10
    healthy_threshold   = 2
    unhealthy_threshold = 2
  }
  lifecycle {
    create_before_destroy = true
  }
}

resource "aws_lb_listener" "models" {
  load_balancer_arn = aws_lb.models.arn
  port              = 8080
  protocol          = "TCP"
  default_action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.models.arn
  }
}

# ---- model_tier = "mock": the Round 1 mock provider as a small Fargate service -------
resource "aws_ecs_task_definition" "mock" {
  count                    = var.model_tier == "mock" ? 1 : 0
  family                   = "mock-provider"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = 512
  memory                   = 1024
  execution_role_arn       = aws_iam_role.execution.arn
  runtime_platform {
    operating_system_family = "LINUX"
    cpu_architecture        = "X86_64"
  }
  container_definitions = jsonencode([{
    name         = "mock-provider"
    image        = local.app_image
    essential    = true
    command      = ["voice-agent", "mock", "--host", "0.0.0.0", "--port", "8080"]
    portMappings = [{ containerPort = 8080, protocol = "tcp" }]
    environment  = [{ name = "MOCK_FAILURE_RATE", value = tostring(var.failure_rate) }]
    logConfiguration = {
      logDriver = "awslogs"
      options = {
        awslogs-group         = aws_cloudwatch_log_group.app.name
        awslogs-region        = var.region
        awslogs-stream-prefix = "mock-provider"
      }
    }
  }])
}

resource "aws_ecs_service" "mock" {
  count           = var.model_tier == "mock" ? 1 : 0
  name            = "mock-provider"
  cluster         = aws_ecs_cluster.main.id
  task_definition = aws_ecs_task_definition.mock[0].arn
  desired_count   = 1
  launch_type     = "FARGATE"
  network_configuration {
    subnets          = aws_subnet.private[*].id
    security_groups  = [aws_security_group.models.id]
    assign_public_ip = false
  }
  load_balancer {
    target_group_arn = aws_lb_target_group.models.arn
    container_name   = "mock-provider"
    container_port   = 8080
  }
  depends_on = [aws_lb_listener.models]
}
