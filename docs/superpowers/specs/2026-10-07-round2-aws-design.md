# Round 2: production-shaped AWS deployment (ap-south-1) — Design

Date: 2026-10-07 · Status: draft for approval

## Goal

Deploy the Round 1 service to a fresh AWS account in ap-south-1 as a production-shaped system:
everything from `terraform apply`, nothing from the console. It runs the brief's traffic profile
against real open-weight STT/LLM/TTS models on one GPU instance, with Round 1's 20% fault
injection in front of them. It autoscales on in-flight calls, is observable from one CloudWatch
dashboard, and is torn down with `terraform destroy` plus a log proving that nothing billable
remains.

## Architecture

```
 laptop load generator ──HTTP──► ALB (public subnets, SG: allowed CIDRs only)
                                   │  sticky sessions: one call stays on one task
                                   ▼
             ECS Fargate service "voice-agent" (private subnets, 2 AZs, min 2 / max 10 tasks)
               │  ActiveCalls (EMF) ──► target tracking   + scheduled pre-warm for campaign window
               │  DynamoDB (gateway endpoint): turns + dead letters, transactional
               ▼
             internal NLB ──► GPU host (private subnet, ASG min=max=1, g5.xlarge, Deep Learning AMI)
                                docker compose:
                                  chaos-proxy :8080  (20% injected failures, outage switch) ──┐
                                  vLLM        :8000  Qwen2.5-7B-Instruct-AWQ  ◄───────────────┤
                                  Speaches    :8001  faster-whisper (STT) + Kokoro (TTS) ◄────┘
 NAT gateway (1) for egress: image + model downloads; S3/DynamoDB gateway endpoints bypass it
```

## Decisions

1. **Model tier: EC2 + docker compose, not ECS-on-EC2 or SageMaker.** Only one GPU fits the
   quota, and three model servers have to share it. ECS assigns a GPU to one container
   exclusively, and SageMaker needs one GPU instance per endpoint (with its own quota). Docker
   compose shares a single GPU across containers. An ASG (min = max = 1) keeps the host
   self-healing, and an internal NLB gives it a stable address.
2. **Fault injection: the Round 1 mock grows a proxy mode.** It applies the same 20% failure
   mix, then forwards healthy requests to the real servers by path: `/v1/chat/completions` goes
   to vLLM, `/v1/audio/*` to Speaches. The service talks to it with the `openai` adapters, which
   is exactly the Round 1 swap story.
3. **Persistence: DynamoDB replaces SQLite.** Fargate disks are per-task and ephemeral, so
   SQLite would lose dead letters on every scale-in.
   - One table holds turns and dead letters. `TransactWriteItems` keeps "turn degraded +
     dead letter written" atomic, and conditional expressions replace the SQL status guards.
   - SQS (Appendix F's suggestion) was considered and rejected for the system of record: a send
     cannot join the DynamoDB transaction. That reintroduces the "turn marked degraded, no dead
     letter" gap Round 1 closed.
   - `Store` becomes a protocol with SQLite (local) and DynamoDB (AWS) backends. Both must pass
     the same contract tests (DynamoDB via moto).
4. **Autoscaling: on ActiveCalls per task.** This is the number of calls each task currently
   holds.
   - ALB stickiness keeps each call on one task, mirroring a WebSocket per call.
   - The service emits the metric via CloudWatch Embedded Metric Format (EMF).
   - Target tracking targets 10 calls per task, with min 2 tasks (one per AZ) and max 10.
   - A scheduled action pre-warms before the campaign window, following the Round 1 cost
     recommendation.
5. **Observability: EMF for custom metrics, plus ALB and Container Insights.** EMF needs no
   `PutMetricData` permission, and CloudWatch computes p50/p90/p99 from the raw values.
   - One dashboard covers:
     - traffic: request rate, error and degraded rate, latency percentiles;
     - capacity: ActiveCalls, desired vs running tasks, autoscaling activity (EventBridge ECS
       events into a log group);
     - dependencies: breaker state per stage, provider error rate, DLQ depth, GPU host health.
   - Alarms: p99 > 3× baseline (the runbook trigger), 5xx rate, breaker open, DLQ growth.
6. **IAM: custom policies only, with explicit actions.**
   - `scripts/check_iam.py` scans `terraform show -json` and fails on any `*` in an action.
   - Resources are scoped to ARNs everywhere except where AWS requires `*`
     (`ecr:GetAuthorizationToken`).
7. **Cost guardrails, built into the IaC.**
   - An AWS Budget with email alerts.
   - The GPU ASG gets a scheduled scale-to-zero N hours after apply, as a dead-man switch.
   - `scripts/teardown_check` writes a log proving that no instances, NAT gateways, load
     balancers, Elastic IPs, volumes or ENIs remain.
8. **State and secrets.** Terraform state is local (git-ignored); an S3 backend with lockfile
   is documented for teams. No secrets: model servers need no keys, and the deployer uses its
   own CLI profile.

## Model host details (verified 2026-10-07)

- **AMI:** Deep Learning Base OSS AMI (AL2023), resolved at apply time from SSM
  `/aws/service/deeplearning/ami/x86_64/base-oss-nvidia-driver-gpu-amazon-linux-2023/latest/ami-id`.
  It ships the NVIDIA driver, Docker and the NVIDIA Container Toolkit. The root volume is
  raised from 75 GB to 100 GB. User-data installs the `docker compose` plugin if missing.
- **One GPU shared by compose services:** each service reserves the GPU (`driver: nvidia`,
  `device_ids: ["0"]`); reservations are not exclusive. VRAM is budgeted by hand on the
  A10G's ~22.5 GiB:
  - **vLLM** `vllm/vllm-openai:v0.31.0` (~8.4 GB image) runs Qwen2.5-7B-Instruct-AWQ with
    `--quantization awq_marlin --gpu-memory-utilization 0.70 --max-model-len 8192
    --max-num-seqs 16`, which is about 15.7 GiB. It starts first.
  - **Speaches** `ghcr.io/speaches-ai/speaches:latest-cuda` (~2.9 GB image) uses the remaining
    ~6.5 GiB:
    - `PRELOAD_MODELS` loads faster-whisper-small and Kokoro-82M ONNX;
    - `STT_MODEL_TTL=-1` and `TTS_MODEL_TTL=-1` stop them unloading after 5 min.
    - Actual VRAM use is checked with `nvidia-smi` on first boot.
- **Download per boot:** about 11 GB of images plus about 7 GB of weights. That matches the
  ~40 GB of NAT data in the cost estimate.
- **Autoscaling metric:** `ActiveCalls` is emitted with only the `ServiceName` dimension
  (never TaskId), and target tracking uses `Average`. EMF lines flush every 5 s or less.
- **Terraform:** AWS provider version 6.6 or later, for native ECS blue/green. The app image is
  built and pushed by `kreuzwerker/docker` 4.6, which on Windows needs
  `host = "npipe:////./pipe/docker_engine"`, and is authenticated via
  `aws_ecr_authorization_token`. That keeps "one `terraform apply`" true.

## Risks

- **LLM throughput at peak (unverified for A10G).**
  - About 75 concurrent calls with turns 30 s apart is about 2.5 LLM requests/s.
  - At ~100 output tokens each, that is about 250 tok/s, near the estimated ceiling.
  - Mitigations:
    - cap `max_tokens` at about 80 (voice replies are short);
    - measure on the first boot;
    - if the GPU saturates, lengthen the per-turn gap rather than cut the call rate.
  - Separately, queueing latency must not be mistaken for provider failure, so the LLM stage
    timeout is set from measured p99.
- **GPU quota:** new accounts start at 0 vCPUs. Request it on day one.

## Demo traffic profile (time-compressed)

The demo compresses the daily cycle into one scripted run:

| Phase | Duration | Rate |
|---|---|---|
| Baseline | 10 min | 10 calls/min |
| Campaign spike | 15 min | 50 calls/min (scheduled pre-warm 2 min before) |
| Cool-down | 10 min | 10 calls/min |

- **Calls:** each call is 3 turns about 30 s apart, using real WAV utterances.
- **Concurrency:** a call lasts about 66 s (two 30 s gaps plus three turns). By Little's law
  that is about 11 concurrent calls at baseline and about 55 at peak, which at 10 calls per
  task should take the service from 2 tasks (the floor) to about 6.
- **Fault injection:** 20% throughout, plus a forced LLM outage during the spike to show the
  breaker and the DLQ, then a replay.

## Cost estimate (ap-south-1 on-demand, Price List checked 2026-10-07)

Usage assumed: the stack is up for about 6 h in total (build/debug, a rehearsal, the recorded
demo). The GPU host runs about 5 h and boots twice, and Fargate averages 4 tasks.

| Item | Unit price | Usage | Cost |
|---|---|---|---|
| g5.xlarge | $1.208/h | 5 h | $6.04 |
| NAT gateway, hourly | $0.056/h | 6 h | $0.34 |
| NAT gateway, data | $0.056/GB | ~40 GB (images + model weights, 2 boots) | $2.24 |
| Fargate 1 vCPU / 2 GB | $0.05187/task-h | ~24 task-h | $1.25 |
| CloudWatch (EMF metrics, Container Insights, logs) | $0.30/metric-month, prorated hourly; 5 GB logs free | ~30 metrics, <1 GB logs | ~$1.00 |
| ALB + internal NLB | $0.0239/h each + LCU | 6 h | ~$0.40 |
| Public IPv4 (NAT EIP + 2 ALB) | $0.005/h each | 6 h | $0.09 |
| EBS gp3 (GPU root, 100 GB) | $0.0912/GB-month | 6 h | $0.08 |
| DynamoDB on-demand, ECR, data out, Budgets | — | tiny / free tier | ~$0.05 |
| **Subtotal** | | | **~$11.50** |
| **With 20% contingency** | | | **~$14 (≈ ₹1,200)**, under the ₹2,500 cap |

- The GPU and the NAT data dominate, so each extra hour of GPU costs about $1.30.
- The two levers are destroying between sessions and the GPU dead-man switch.
- g5 is offered in only two Mumbai AZs (IDs aps1-az1 and aps1-az3), so subnets are chosen
  by AZ ID, not by name.

## Stretch

- **Multi-AZ kill:** stop every task in one AZ mid-load; the ALB keeps serving and ECS
  replaces the tasks.
- **Blue/green:** ECS native blue/green, if the provider supports it.
- **ap-south-1 notes:** borrower-PII residency (DPDP Act 2023, RBI localisation for payment
  data), in-region self-hosted models, latency to Indian metros, ap-south-2 (Hyderabad) as an
  in-country DR region.

## Deliverables mapping

- **Repo:** `infra/` (Terraform), app changes, README.
- **Runbook:** `RUNBOOK.md`, based on Appendix I.
- **Demo:** a 5-minute shot list for your screen recording.
- **Cost write-up:** revised with measured numbers from Cost Explorer and CloudWatch.
- **Reimbursement:** the billing PDF comes from you.
- **Teardown:** confirmation log and screenshot.
