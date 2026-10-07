data "aws_ssm_parameter" "dlami" {
  name = "/aws/service/deeplearning/ami/x86_64/base-oss-nvidia-driver-gpu-amazon-linux-2023/latest/ami-id"
}

locals {
  registry = split("/", aws_ecr_repository.app.repository_url)[0]
  compose = templatefile("${path.module}/templates/compose.yaml.tftpl", {
    vllm_image     = var.vllm_image
    speaches_image = var.speaches_image
    proxy_image    = local.app_image
    llm_hf_model   = var.llm_hf_model
    llm_model      = var.llm_model
    stt_model      = var.stt_model
    tts_model      = var.tts_model
    failure_rate   = var.failure_rate
    log_group      = aws_cloudwatch_log_group.gpu.name
    region         = var.region
  })
}

resource "aws_launch_template" "gpu" {
  count                  = var.model_tier == "gpu" ? 1 : 0
  name_prefix            = "${local.name}-gpu-"
  image_id               = data.aws_ssm_parameter.dlami.insecure_value
  instance_type          = var.gpu_instance_type
  vpc_security_group_ids = [aws_security_group.models.id]
  iam_instance_profile {
    arn = aws_iam_instance_profile.gpu.arn
  }
  metadata_options {
    http_tokens                 = "required" # IMDSv2 only
    http_put_response_hop_limit = 1
  }
  block_device_mappings {
    device_name = "/dev/xvda"
    ebs {
      volume_size           = 100
      volume_type           = "gp3"
      encrypted             = true
      delete_on_termination = true
    }
  }
  user_data = base64encode(templatefile("${path.module}/templates/gpu-user-data.sh.tftpl", {
    compose         = local.compose
    region          = var.region
    registry        = local.registry
    compose_version = var.compose_version
  }))
  tag_specifications {
    resource_type = "instance"
    tags          = { Name = "${local.name}-gpu" }
  }
  tag_specifications {
    resource_type = "volume"
    tags          = { Name = "${local.name}-gpu" }
  }
}

resource "aws_autoscaling_group" "gpu" {
  count                     = var.model_tier == "gpu" ? 1 : 0
  name                      = "${local.name}-gpu"
  min_size                  = 1
  max_size                  = 1
  desired_capacity          = 1
  vpc_zone_identifier       = aws_subnet.private[*].id # both subnets are in g5 AZs
  target_group_arns         = [aws_lb_target_group.models.arn]
  health_check_type         = "EC2" # don't kill the host while 18 GB of models load
  health_check_grace_period = 1200
  launch_template {
    id      = aws_launch_template.gpu[0].id
    version = aws_launch_template.gpu[0].latest_version
  }
  instance_refresh {
    strategy = "Rolling"
    preferences {
      min_healthy_percentage = 0
    }
  }
  tag {
    key                 = "Name"
    value               = "${local.name}-gpu"
    propagate_at_launch = true
  }
}

# Dead-man switch: the GPU scales to zero gpu_max_hours after creation, even if nobody
# remembers to destroy. Extend with: terraform apply -replace='aws_autoscaling_schedule.gpu_off[0]'
resource "aws_autoscaling_schedule" "gpu_off" {
  count                  = var.model_tier == "gpu" ? 1 : 0
  scheduled_action_name  = "dead-man-switch"
  autoscaling_group_name = aws_autoscaling_group.gpu[0].name
  start_time             = timeadd(plantimestamp(), "${var.gpu_max_hours}h")
  min_size               = 0
  max_size               = 0
  desired_capacity       = 0
  lifecycle {
    ignore_changes = [start_time]
  }
}
