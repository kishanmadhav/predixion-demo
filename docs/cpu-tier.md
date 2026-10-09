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
| Host | g5.xlarge (A10G 24 GB), Deep Learning AMI | m7a.2xlarge (8 vCPU = 8 physical AMD cores, 32 GB), Amazon Linux 2023, $0.580/h |
| LLM | vLLM, Qwen2.5-7B-Instruct-AWQ | llama.cpp server, Qwen2.5-1.5B-Instruct Q4_K_M (GGUF), 4 threads, 4 parallel slots |
| STT | Speaches CUDA, faster-whisper-small (fp16) | Speaches CPU, faster-whisper-tiny.en (int8), 2 workers |
| TTS | Kokoro-82M ONNX | Kokoro-82M ONNX (same) |
| LLM reply cap | 80 tokens | 40 tokens |
| Stage timeouts (STT / LLM / TTS) | 5 / 8 / 5 s, turn deadline 20 s | 8 / 15 / 12 s, turn deadline 35 s |

The quota fit:
- an m7a.2xlarge is 8 vCPUs, exactly the account's standard on-demand quota (`L-1216C47A` = 8). m7a runs one thread per core, so those are 8 real cores; a c7i.2xlarge is 4 cores with 2 threads each, for about $0.11/h less;
- the voice-agent tasks run on Fargate, which has its own 6-vCPU quota.

## What is unchanged

Everything the brief evaluates apart from the hardware:

- **Real open-weight models.** The service calls them over HTTP through the `openai` adapters. The models are smaller, but they are still real models and not a mock.
- **The chaos proxy.** It runs on the host in front of the models, with 20% injected failures and the outage switch. `scripts/chaos.py` drives it over SSM.
- **The rest of the stack is the same Terraform.** That covers Fargate behind the ALB, private subnets with NAT, least-privilege IAM (`check_iam.py`), ActiveCalls target tracking, the CloudWatch dashboard and alarms, the dead-man switch, and teardown with `teardown_check.py`.
- **Switching back to the GPU is one variable.** When the quota is granted, `model_tier = "gpu"` deploys the original design unchanged.

## Effect on the load test

A CPU serves far fewer requests per second than an A10G, so the campaign is scaled down; the mechanisms stay the same.

**Measured locally, 2026-10-09.** The same compose stack ran in Docker Desktop on a 16-thread laptop. Each figure is wall time for N parallel requests.

| Stage | 1 request | 8 in parallel | Throughput |
|---|---|---|---|
| STT, faster-whisper-base, 1 worker | 0.9 s | 6.9 s | ~1.2/s, serialized |
| STT, faster-whisper-tiny.en, 2-4 workers | 0.5 s | 2.3 s | ~3.5/s |
| LLM, Qwen2.5-1.5B Q4_K_M, 4 slots, 48 tokens | 1.2 s | 3.9 s | ~2/s |
| TTS, Kokoro-82M ONNX, one sentence | 1.3 s | 5.6 s | ~1.4/s |

**What the measurements changed:**
- **STT model.** faster-whisper-base with the default single worker was the bottleneck. A 3-minute local run at 20 calls/min degraded 61% of turns: STT timeouts opened its breaker. Switching to tiny.en with 2 workers fixed this. It transcribes the demo utterances exactly.
- **Model installation.** This Speaches release ignores `PRELOAD_MODELS`. Models are now installed through its API by a one-shot `speaches-models` container, which also warms the TTS model. Without it, every STT and TTS call returned 404. The GPU compose file had the same bug and gets the same fix.
- **Traffic profile.** TTS is now the slowest stage. A turn runs STT, then LLM, then TTS. Local 3-minute runs with 20% injected failures:

  | Spike rate | Turns | Degraded | HTTP failures | p50 / p99 | Bottleneck |
  |---|---|---|---|---|---|
  | 12 calls/min, TTS timeout 8 s | 111 | 35% | 0 | 16.1 / 30.3 s | TTS queues past its timeout; its breaker opens |
  | 8 calls/min, TTS timeout 12 s, 40-token replies | 84 | 6% | 0 | 10.7 / 37.1 s | none: degraded turns are the injected failures that outlast retries |

  - The default `baseline:10:10,spike:15:50,cooldown:10:10` becomes `baseline:10:3,spike:15:8,cooldown:10:3`.
  - A call lasts about 90 s (two 30 s gaps plus three slow turns). By Little's law that is about 4-5 concurrent calls at baseline and about 12 at the peak.
- **Autoscaling target.** `target_active_calls = 2`, so 12 calls means about 6 tasks. The service scales from the 2-task floor to well above it, which is what the demo has to show; only the absolute numbers are smaller.
- **Rehearsal on AWS first.** A short run (`smoke:3:8`) checks these numbers on the m7a host. The stage timeouts and `baseline_p99_ms` are then set from what was measured, and the measured results go into the README.
- **Latency is the cost of CPU inference.** Uncontended, the three stages take about 0.5 + 1.2 + 1.3 s. Under load, the p50 includes queueing behind other calls, retries after injected failures, and injected hangs that wait out a stage timeout.

## What this does not show

- **GPU behaviour.** Sharing VRAM between vLLM and Speaches, and vLLM's continuous batching under real concurrency, are untested. The GPU compose file and VRAM budget are in [`infra/templates/compose.yaml.tftpl`](../infra/templates/compose.yaml.tftpl), unexercised.
- **Latency.** Absolute latencies are CPU latencies. They are not representative of the production design, so the p99 alarm threshold is set from the measured CPU baseline.
