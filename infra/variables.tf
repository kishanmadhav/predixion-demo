variable "region" {
  type    = string
  default = "ap-south-1"
}

variable "aws_profile" {
  description = "AWS CLI profile for this project only."
  type        = string
  default     = "predixion"
}

variable "docker_host" {
  description = "Docker daemon. Windows: npipe:////./pipe/docker_engine; Linux/macOS: unix:///var/run/docker.sock"
  type        = string
  default     = "npipe:////./pipe/docker_engine"
}

variable "az_ids" {
  description = "AZ IDs (not names: names differ per account). g5 is offered only in aps1-az1 and aps1-az3."
  type        = list(string)
  default     = ["aps1-az1", "aps1-az3"]
}

variable "allowed_cidrs" {
  description = "CIDRs allowed to reach the ALB (your IP as x.x.x.x/32)."
  type        = list(string)
}

variable "model_tier" {
  description = "mock: mock provider on Fargate (cheap infra check). gpu: g5.xlarge with vLLM + Speaches."
  type        = string
  default     = "mock"
  validation {
    condition     = contains(["mock", "gpu"], var.model_tier)
    error_message = "model_tier must be \"mock\" or \"gpu\"."
  }
}

variable "task_cpu" {
  type    = number
  default = 1024
}

variable "task_memory" {
  type    = number
  default = 2048
}

variable "min_tasks" {
  type    = number
  default = 2
}

variable "max_tasks" {
  type    = number
  default = 10
}

variable "target_active_calls" {
  description = "Target tracking: average active calls per task."
  type        = number
  default     = 10
}

variable "campaign_prewarm" {
  description = "Optional scheduled pre-warm: start/end are at(...) or cron(...) expressions in UTC."
  type = object({
    start     = string
    end       = string
    min_tasks = number
  })
  default = null
}

variable "failure_rate" {
  description = "Injected provider failure rate (Round 1: 20%)."
  type        = number
  default     = 0.2
}

variable "turn_deadline_s" {
  type    = number
  default = 20
}

variable "stage_timeouts_s" {
  description = "Per-attempt timeouts; set from measured p99 on the GPU (Task 13)."
  type        = object({ stt = number, llm = number, tts = number })
  default     = { stt = 5, llm = 8, tts = 5 }
}

variable "llm_hf_model" {
  type    = string
  default = "Qwen/Qwen2.5-7B-Instruct-AWQ"
}

variable "llm_model" {
  description = "Name vLLM serves the model under."
  type        = string
  default     = "qwen2.5-7b-instruct"
}

variable "stt_model" {
  type    = string
  default = "Systran/faster-whisper-small"
}

variable "tts_model" {
  type    = string
  default = "speaches-ai/Kokoro-82M-v1.0-ONNX"
}

variable "tts_voice" {
  type    = string
  default = "af_heart"
}

variable "vllm_image" {
  type    = string
  default = "vllm/vllm-openai:v0.31.0"
}

variable "speaches_image" {
  type    = string
  default = "ghcr.io/speaches-ai/speaches:latest-cuda-12.6.3"
}

variable "gpu_instance_type" {
  type    = string
  default = "g5.xlarge"
}

variable "gpu_max_hours" {
  description = "Dead-man switch: the GPU ASG scales to zero this many hours after it is created."
  type        = number
  default     = 4
}

variable "compose_version" {
  type    = string
  default = "v2.39.4"
}

variable "alert_email" {
  description = "Optional email for alarm notifications (confirm the SNS subscription email)."
  type        = string
  default     = null
}

variable "baseline_p99_ms" {
  description = "Measured p99 turn latency at baseline; the runbook trigger is 3x this."
  type        = number
  default     = 3000
}

variable "log_retention_days" {
  type    = number
  default = 7
}
