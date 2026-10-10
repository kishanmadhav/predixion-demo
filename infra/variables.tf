variable "region" {
  type    = string
  default = "ap-southeast-2"
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
  description = "AZ IDs (not names: names differ per account). g5 is offered only in apse2-az1 and apse2-az2 (ap-southeast-2); in ap-south-1 use aps1-az1 and aps1-az3."
  type        = list(string)
  default     = ["apse2-az1", "apse2-az2"]
}

variable "allowed_cidrs" {
  description = "CIDRs allowed to reach the ALB (your IP as x.x.x.x/32)."
  type        = list(string)
  validation {
    condition     = alltrue([for c in var.allowed_cidrs : !contains(["0.0.0.0/0", "::/0"], c)])
    error_message = "allowed_cidrs must not contain 0.0.0.0/0 or ::/0: the demo ALB is unauthenticated, so use your own IP as x.x.x.x/32."
  }
}

variable "model_tier" {
  description = "mock: mock provider on Fargate (cheap infra check). gpu: g5.xlarge with vLLM + Speaches. cpu: m7a.2xlarge with llama.cpp + Speaches (no-GPU-quota fallback, docs/cpu-model-tier.md)."
  type        = string
  default     = "mock"
  validation {
    condition     = contains(["mock", "gpu", "cpu"], var.model_tier)
    error_message = "model_tier must be \"mock\", \"gpu\" or \"cpu\"."
  }
}

variable "task_cpu" {
  description = "0.5 vCPU: 10 tasks plus the mock provider stay under a new account's 6-vCPU Fargate quota."
  type        = number
  default     = 512
}

variable "task_memory" {
  type    = number
  default = 1024
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
  description = "Whole-turn deadline. null: 20 s (mock, gpu) or 35 s (cpu, slower models)."
  type        = number
  default     = null
}

variable "stage_timeouts_s" {
  description = "Per-attempt timeouts; set from measured p99. null: per-tier defaults in ecs.tf."
  type        = object({ stt = number, llm = number, tts = number })
  default     = null
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

variable "model_host_max_hours" {
  description = "Dead-man switch: the model host ASG (gpu or cpu tier) scales to zero this many hours after it is created."
  type        = number
  default     = 3
}

variable "cpu_instance_type" {
  description = "cpu tier host. 8 vCPUs fits a new account's 8-vCPU standard on-demand quota; m7a has one thread per core, so 8 vCPUs are 8 real cores (c7i.2xlarge is 4 cores x 2 threads)."
  type        = string
  default     = "m7a.2xlarge"
}

variable "llama_image" {
  type    = string
  default = "ghcr.io/ggml-org/llama.cpp:server-b11515"
}

variable "cpu_llm_gguf" {
  description = "Hugging Face GGUF repo:quant that llama.cpp downloads (-hf)."
  type        = string
  default     = "Qwen/Qwen2.5-1.5B-Instruct-GGUF:Q4_K_M"
}

variable "cpu_llm_model" {
  description = "Name llama.cpp serves the cpu-tier model under."
  type        = string
  default     = "qwen2.5-1.5b-instruct"
}

variable "cpu_llm_threads" {
  type    = number
  default = 4
}

variable "cpu_llm_parallel" {
  description = "llama.cpp parallel slots (concurrent requests, continuously batched)."
  type        = number
  default     = 4
}

variable "cpu_stt_model" {
  description = "tiny.en: about 3x the CPU throughput of base, accurate on the demo utterances (measured 2026-10-09)."
  type        = string
  default     = "Systran/faster-whisper-tiny.en"
}

variable "cpu_speaches_image" {
  type    = string
  default = "ghcr.io/speaches-ai/speaches:latest-cpu"
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
