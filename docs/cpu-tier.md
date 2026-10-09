# Deviation: CPU model tier instead of a GPU

**Status:** approved by Predixion (Juhi Maheshwari, 2026-10-09: "You may work with the alternate approach but please document the same.")

## What the brief asked for

A live load test against real open-weight STT/LLM/TTS models on a GPU (g5.xlarge), with 20% failure injection in front of them.

## Why there is no GPU

- **New accounts start with zero GPU quota.** In the deployment account (ap-southeast-2) every GPU quota was 0 on 2026-10-08:
  - EC2 "Running On-Demand G and VT instances" (`L-DB2E81BA`);
  - G and VT Spot, P on-demand and P Spot;
  - every SageMaker `ml.g4dn` / `ml.g5` quota.
- **AWS declined the increase.** A request for 4 vCPUs (`L-DB2E81BA`, support case 179144875100415) was declined: "Service quotas are put in place to help you gradually ramp up activity". An appeal with a detailed use case was filed and is still pending.
- **No other region helps.** The organization's region-restriction SCP allows workloads only in ap-southeast-2, so a GPU quota in another region would not be usable either.

## What runs instead

`model_tier = "cpu"` keeps the GPU tier's shape and swaps only what runs on the host:

| | `gpu` tier (designed) | `cpu` tier (deployed) |
|---|---|---|
| Host | g5.xlarge (A10G 24 GB), Deep Learning AMI | c7i.2xlarge (8 vCPU, 16 GB), Amazon Linux 2023, $0.466/h |
| LLM | vLLM, Qwen2.5-7B-Instruct-AWQ | llama.cpp server, Qwen2.5-1.5B-Instruct Q4_K_M (GGUF), 4 threads, 4 parallel slots |
| STT | Speaches CUDA, faster-whisper-small (fp16) | Speaches CPU, faster-whisper-base (int8) |
| TTS | Kokoro-82M ONNX | Kokoro-82M ONNX (same) |
| LLM reply cap | 80 tokens | 48 tokens |
| Stage timeouts (STT / LLM / TTS) | 5 / 8 / 5 s, turn deadline 20 s | 8 / 15 / 8 s, turn deadline 30 s |

The quota fit:
- a c7i.2xlarge is 8 vCPUs, exactly the account's standard on-demand quota (`L-1216C47A` = 8);
- the voice-agent tasks run on Fargate, which has its own 6-vCPU quota.

## What is unchanged

Everything the brief evaluates apart from the hardware:

- **Real open-weight models.** The service calls them over HTTP through the `openai` adapters. The models are smaller, but they are still real models and not a mock.
- **The chaos proxy.** It runs on the host in front of the models, with 20% injected failures and the outage switch. `scripts/chaos.py` drives it over SSM.
- **The rest of the stack is the same Terraform.** That covers Fargate behind the ALB, private subnets with NAT, least-privilege IAM (`check_iam.py`), ActiveCalls target tracking, the CloudWatch dashboard and alarms, the dead-man switch, and teardown with `teardown_check.py`.
- **Switching back to the GPU is one variable.** When the quota is granted, `model_tier = "gpu"` deploys the original design unchanged.

## Effect on the load test

A CPU serves far fewer tokens per second than an A10G, so the campaign is scaled down, not the mechanisms. Throughput was not measured before the demo. The plan:

- **Traffic profile.** The default `baseline:10:10,spike:15:50,cooldown:10:10` becomes `baseline:10:4,spike:15:20,cooldown:10:4`.
  - At the spike that is about 1 turn per second at the model host.
  - At about 75 s per call (3 turns, 30 s apart), Little's law gives about 5 concurrent calls at baseline and about 25 at peak.
- **Autoscaling target.** `target_active_calls = 4`, so 25 calls means about 7 tasks. The service still scales from the 2-task floor to well above it, which is what the demo has to show. Only the absolute numbers are smaller.
- **Rehearsal first.** A short rehearsal (`smoke:3:12`) measures the per-stage p99. If the host saturates, the spike rate is lowered (for example to 12 calls/min with `target_active_calls = 3`), and the stage timeouts and `baseline_p99_ms` are set from what was measured. The measured numbers go into the README cost and results sections.

## What this does not show

- **GPU behaviour.** Sharing VRAM between vLLM and Speaches, and vLLM's continuous batching under real concurrency, are untested. The GPU compose file and VRAM budget are in [`infra/templates/compose.yaml.tftpl`](../infra/templates/compose.yaml.tftpl), unexercised.
- **Latency.** Absolute latencies are CPU latencies. They are not representative of the production design, so the p99 alarm threshold is set from the measured CPU baseline.
