# The model host: one EC2 instance in an ASG (min = max = 1) running the models and the
# chaos proxy under docker compose, behind the internal NLB.
#   gpu: g5.xlarge, Deep Learning AMI, vLLM (Qwen2.5-7B-AWQ) + Speaches CUDA.
#   cpu: c7i.2xlarge, Amazon Linux 2023, llama.cpp (Qwen2.5-1.5B Q4_K_M) + Speaches CPU.
#        The fallback when the account has no GPU quota; see docs/cpu-tier.md.

data "aws_ssm_parameter" "dlami" {
  name = "/aws/service/deeplearning/ami/x86_64/base-oss-nvidia-driver-gpu-amazon-linux-2023/latest/ami-id"
}

data "aws_ssm_parameter" "al2023" {
  name = "/aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-x86_64"
}

locals {
  host_tier = var.model_tier != "mock"
  gpu       = var.model_tier == "gpu"
  registry  = split("/", aws_ecr_repository.app.repository_url)[0]

  # Model names the app asks for, per tier.
  llm_model = local.gpu ? var.llm_model : var.cpu_llm_model
  stt_model = local.gpu ? var.stt_model : var.cpu_stt_model

  compose = local.gpu ? templatefile("${path.module}/templates/compose.yaml.tftpl", {
    vllm_image     = var.vllm_image
    speaches_image = var.speaches_image
    proxy_image    = local.app_image
    llm_hf_model   = var.llm_hf_model
    llm_model      = var.llm_model
    stt_model      = var.stt_model
    tts_model      = var.tts_model
    failure_rate   = var.failure_rate
    log_group      = aws_cloudwatch_log_group.model_host.name
    region         = var.region
    }) : templatefile("${path.module}/templates/compose-cpu.yaml.tftpl", {
    llama_image    = var.llama_image
    speaches_image = var.cpu_speaches_image
    proxy_image    = local.app_image
    llm_gguf       = var.cpu_llm_gguf
    llm_model      = var.cpu_llm_model
    llm_threads    = var.cpu_llm_threads
    llm_parallel   = var.cpu_llm_parallel
    stt_model      = var.cpu_stt_model
    tts_model      = var.tts_model
    failure_rate   = var.failure_rate
    log_group      = aws_cloudwatch_log_group.model_host.name
    region         = var.region
  })
}

resource "aws_launch_template" "model_host" {
  count                  = local.host_tier ? 1 : 0
  name_prefix            = "${local.name}-models-"
  image_id               = local.gpu ? data.aws_ssm_parameter.dlami.insecure_value : data.aws_ssm_parameter.al2023.insecure_value
  instance_type          = local.gpu ? var.gpu_instance_type : var.cpu_instance_type
  vpc_security_group_ids = [aws_security_group.models.id]
  iam_instance_profile {
    arn = aws_iam_instance_profile.model_host.arn
  }
  metadata_options {
    http_tokens                 = "required" # IMDSv2 only
    http_put_response_hop_limit = 1
  }
  block_device_mappings {
    device_name = "/dev/xvda"
    ebs {
      volume_size           = local.gpu ? 100 : 40 # GPU: ~11 GB images + ~7 GB weights on a 75 GB AMI. CPU: ~4 GB + ~2 GB.
      volume_type           = "gp3"
      encrypted             = true
      delete_on_termination = true
    }
  }
  user_data = base64encode(templatefile("${path.module}/templates/model-host-user-data.sh.tftpl", {
    gpu             = local.gpu
    compose         = local.compose
    region          = var.region
    registry        = local.registry
    compose_version = var.compose_version
  }))
  tag_specifications {
    resource_type = "instance"
    tags          = { Name = "${local.name}-models" }
  }
  tag_specifications {
    resource_type = "volume"
    tags          = { Name = "${local.name}-models" }
  }
}

resource "aws_autoscaling_group" "model_host" {
  count                     = local.host_tier ? 1 : 0
  name                      = "${local.name}-models-${var.model_tier}"
  min_size                  = 1
  max_size                  = 1
  desired_capacity          = 1
  vpc_zone_identifier       = aws_subnet.private[*].id # both subnets are in AZs that offer g5 and c7i
  target_group_arns         = [aws_lb_target_group.models.arn]
  health_check_type         = "EC2" # don't kill the host while models download and load
  health_check_grace_period = 1200
  launch_template {
    id      = aws_launch_template.model_host[0].id
    version = aws_launch_template.model_host[0].latest_version
  }
  instance_refresh {
    strategy = "Rolling"
    preferences {
      min_healthy_percentage = 0
    }
  }
  tag {
    key                 = "Name"
    value               = "${local.name}-models"
    propagate_at_launch = true
  }
}

# Dead-man switch: the host scales to zero model_host_max_hours after creation, even if nobody
# remembers to destroy. Extend with: terraform apply -replace='aws_autoscaling_schedule.model_host_off[0]'
resource "aws_autoscaling_schedule" "model_host_off" {
  count                  = local.host_tier ? 1 : 0
  scheduled_action_name  = "dead-man-switch"
  autoscaling_group_name = aws_autoscaling_group.model_host[0].name
  start_time             = timeadd(plantimestamp(), "${var.model_host_max_hours}h")
  min_size               = 0
  max_size               = 0
  desired_capacity       = 0
  lifecycle {
    ignore_changes = [start_time]
  }
}
