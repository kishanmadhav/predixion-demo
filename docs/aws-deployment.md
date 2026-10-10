# Deploying to AWS

The full setup for the Round 2 deployment. The [README](../README.md) has the overview; [`RUNBOOK.md`](../RUNBOOK.md) covers operating it.

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
                                  Speaches    :8000  faster-whisper (STT) + Kokoro (TTS) ◄────┘
                              cpu tier: m7a.2xlarge, AL2023, llama.cpp (Qwen2.5-1.5B Q4_K_M) in place of vLLM
 NAT gateway (1) for egress: image + model downloads; S3/DynamoDB gateway endpoints bypass it
```

With `model_tier = "mock"` the internal NLB fronts the Round 1 mock provider on Fargate instead of the model host. That is a cheap way to check all the infrastructure before paying for the models.

**Security posture of the demo.** The demo ALB is HTTP-only and unauthenticated. It is restricted by security group to `allowed_cidrs` (your own /32) and serves synthetic data only (generated utterances, no borrower data). The DLQ and admin endpoints are on that same listener. The production path is an HTTPS listener with an ACM certificate, service-to-service auth (SigV4 or mTLS) or a private ALB behind the telephony layer, and the DLQ and admin endpoints moved off the public listener.

## Prerequisites

- An AWS account. The demo account had a $10 spending cap.
- If the account sits in an AWS Organization, the organization's SCP must allow EC2, ECS, ELB, DynamoDB, CloudWatch, Logs, ECR, SSM and Auto Scaling in ap-southeast-2. Otherwise `apply` fails with access-denied errors that name no policy.
- For `model_tier = "cpu"`: 8 vCPUs of "Running On-Demand Standard" instances (`L-1216C47A`; new accounts get 8).
- For `model_tier = "gpu"`: a quota of at least 4 vCPUs for "Running On-Demand G and VT instances" (quota code `L-DB2E81BA`). New accounts start at 0. Request it on day one, because approval can take a day or more:

  ```bash
  aws service-quotas get-service-quota --service-code ec2 --quota-code L-DB2E81BA --profile predixion --region ap-southeast-2
  aws service-quotas request-service-quota-increase --service-code ec2 --quota-code L-DB2E81BA --desired-value 4 --profile predixion --region ap-southeast-2
  ```
- Docker Desktop, running, for every `terraform` command including `destroy`. Terraform builds and pushes the app image through the docker provider (`docker_host` defaults to the Windows named pipe; on Linux or macOS set `docker_host = "unix:///var/run/docker.sock"`).
- Terraform 1.9 or later, AWS CLI v2, and [uv](https://docs.astral.sh/uv/).

## One-time setup

**1. The `predixion` profile.** All commands use a profile of that name, scoped to this project:

```bash
aws configure --profile predixion          # access key, secret, default region ap-southeast-2, output json
aws sts get-caller-identity --profile predixion
```

**2. A budget.** A $10 monthly cost budget, with email alerts at $3, $5 and $7 of actual spend and at $9 forecast. It is a one-time CLI step, not part of the Terraform. Budgets is a global service served from us-east-1, and the email address is a placeholder.

```bash
aws budgets create-budget --account-id <ACCOUNT_ID> --profile predixion --region us-east-1 \
  --budget '{"BudgetName":"predixion-10usd-cap","BudgetLimit":{"Amount":"10","Unit":"USD"},"TimeUnit":"MONTHLY","BudgetType":"COST"}'
for t in 3 5 7; do
  aws budgets create-notification --account-id <ACCOUNT_ID> --budget-name predixion-10usd-cap --profile predixion --region us-east-1 \
    --notification NotificationType=ACTUAL,ComparisonOperator=GREATER_THAN,Threshold=$t,ThresholdType=ABSOLUTE_VALUE \
    --subscribers SubscriptionType=EMAIL,Address=you@example.com
done
aws budgets create-notification --account-id <ACCOUNT_ID> --budget-name predixion-10usd-cap --profile predixion --region us-east-1 \
  --notification NotificationType=FORECASTED,ComparisonOperator=GREATER_THAN,Threshold=9,ThresholdType=ABSOLUTE_VALUE \
  --subscribers SubscriptionType=EMAIL,Address=you@example.com
```

A budget only alerts. The hard stops are the destroy after every session and the model host's dead-man switch.

**3. `terraform.tfvars`.** Copy the example and edit it (the file is git-ignored):

```bash
cp infra/terraform.tfvars.example infra/terraform.tfvars
```

Set `allowed_cidrs` to your own public IP (`curl -s https://checkip.amazonaws.com` and add `/32`): the ALB accepts traffic only from there. `alert_email` is optional; if set, accept the SNS confirmation email. The variables you are likely to change:

| Variable | Default | Meaning |
|---|---|---|
| `allowed_cidrs` | none (required) | Who can reach the ALB |
| `model_tier` | `mock` | `mock` (Fargate mock provider), `gpu` (g5.xlarge with vLLM and Speaches) or `cpu` (m7a.2xlarge with llama.cpp and Speaches, see [`docs/cpu-model-tier.md`](cpu-model-tier.md)) |
| `min_tasks` / `max_tasks` | 2 / 10 | Fargate task floor and ceiling |
| `target_active_calls` | 10 | Autoscaling target, active calls per task |
| `campaign_prewarm` | none | `{ start, end, min_tasks }`, `at(...)` or `cron(...)` in UTC. Use `min_tasks = 3`: pre-warm the floor, and let ActiveCalls target tracking scale the spike |
| `failure_rate` | 0.2 | Injected provider failure rate (changing it replaces the model host) |
| `baseline_p99_ms` | 3000 | The latency alarm fires above 3x this |
| `model_host_max_hours` | 3 | Dead-man switch: the model host scales to zero this long after creation |
| `stage_timeouts_s` / `turn_deadline_s` | per tier | 5/8/5 s and 20 s for `mock` and `gpu`; 8/15/12 s and 35 s for `cpu` |
| `docker_host` | Windows named pipe | Docker daemon address |

## Deploy

All Terraform commands run from the repo root through `-chdir=infra`. The commands below assume a POSIX shell (Git Bash on Windows). In PowerShell, set the URL with `$URL = terraform -chdir=infra output -raw url` and use `$URL` the same way; `mkdir -p results` becomes `New-Item -ItemType Directory -Force results`. The sequence:

```bash
# 1. Caller utterances (the six WAVs in assets/utterances are committed; this regenerates them, Windows only)
powershell -File scripts/make_utterances.ps1

# 2. Init, then check the IAM policies before anything is created
terraform -chdir=infra init
terraform -chdir=infra plan -out=tfplan
terraform -chdir=infra show -json tfplan > infra/plan.json
uv run python scripts/check_iam.py infra/plan.json     # fails on any "*" action or AWS-managed policy

# 3. Apply the cheap mock tier first, to prove the infrastructure
terraform -chdir=infra apply tfplan
URL=$(terraform -chdir=infra output -raw url)

# 4. Smoke test: a short campaign (1 minute, 10 calls/min, 2 turns, 5 s apart)
curl -s $URL/health
uv run voice-agent campaign --url $URL --profile smoke:1:10 --turns 2 --gap 5

# 5. Switch to the model host. GPU: model_tier = "gpu" in infra/terraform.tfvars
#    (docker manifest inspect vllm/vllm-openai:v0.31.0 first: the image tag must exist).
#    CPU (what the demo ran, docs/cpu-model-tier.md): model_tier = "cpu" and target_active_calls = 2.
terraform -chdir=infra apply
# The host downloads its images and model weights before it is ready: allow 15-20 minutes (gpu,
# about 18 GB) or about 10 minutes (cpu, about 6 GB) before the "Model tier health (NLB healthy
# hosts)" widget shows 1.

# 6. The campaign. GPU: 10 min at 10 calls/min, 15 min at 50 calls/min, 10 min at 10 calls/min.
#    Pre-warm the floor first: campaign_prewarm in terraform.tfvars with min_tasks = 3 (not 6), then apply.
#    The spike must be absorbed by ActiveCalls target tracking scaling above that floor; that is what the demo shows.
mkdir -p results
uv run voice-agent campaign --url $URL --json-out results/campaign.json
#    CPU: rehearse first to measure per-stage latency, then run the scaled-down profile.
uv run voice-agent campaign --url $URL --profile smoke:3:8
uv run voice-agent campaign --url $URL --profile baseline:10:3,spike:15:8,cooldown:10:3 --json-out results/campaign.json

# 7. While it runs (during the spike): chaos, then replay
uv run python scripts/chaos.py outage --stage llm --seconds 60
# Replay once every breaker is closed (poll `curl -s $URL/health`, or watch the BreakerState widget),
# in batches: the ALB closes a request after 60 s idle, so one big replay would return 504.
curl -s -X POST "$URL/v1/dlq/replay?limit=10"      # 10 on the CPU tier, 50 on GPU; repeat until 0 pending
curl -s "$URL/v1/dlq?status=pending&limit=1"       # "counts" holds the pending total

# 8. Tear down, then prove it
terraform -chdir=infra destroy
uv run python scripts/teardown_check.py
```

Notes:
- If `teardown_check.py` reports only a leftover Container Insights log group (`/aws/ecs/containerinsights/...`), it is a late flush that recreated the group after the destroy. Wait 5 minutes, delete it with `aws logs delete-log-group --log-group-name <name> --profile predixion --region ap-southeast-2`, and re-run the check.
- The first `apply` also builds and pushes the app image (a few minutes) and waits for the ECS service to reach steady state.
- `chaos.py` works only with `model_tier = "gpu"` or `"cpu"` (it reaches the chaos proxy over SSM). Flags: `stats`, `reset`, `outage --stage {stt,llm,tts} --seconds N`, `rate --rate 0.2 [--stage ...]`.
- The campaign `--profile` option is the traffic profile as `name:minutes:calls_per_min,...`, not an AWS profile. Its exit code is 1 if any HTTP request failed; the target is 0 failures.
- Some policy values are only known after apply. If the check prints "resolve at apply", re-run it on the state once the stack is up: `terraform -chdir=infra show -json > infra/plan.json`, then `uv run python scripts/check_iam.py infra/plan.json`.
- Applying `tfplan` (step 3) uses the exact plan that was IAM-checked. Later applies prompt for confirmation.
- `terraform output` shows `url`, `dashboard_url`, `cluster`, `models_endpoint`, `model_host_asg`, `state_table` and `app_image`.
- `teardown_check.py` writes `teardown/teardown-check-<UTC>.log` and exits 0 only when nothing is left in the region (instances, NAT gateways, load balancers, Elastic IPs, volumes, ENIs, and the rest). Commit that log.
- Operating it, and what changes are expensive to apply, is in [`RUNBOOK.md`](../RUNBOOK.md).

## Cost

Estimated, not measured. ap-southeast-2 on-demand prices from the AWS Price List API, checked on 2026-10-08 and 2026-10-09. Both plans fit the account's **$10 cap**. The stack is up for about 5 hours in total: about 1 hour on the mock tier to check the infrastructure, then one model-host session in which the host runs about 3 hours and boots once. Fargate averages 5 tasks of 0.5 vCPU / 1 GB.

| Item | Unit price | Usage | CPU tier (deployed) | GPU tier (designed) |
|---|---|---|---|---|
| Model host | m7a.2xlarge $0.580/h; g5.xlarge $1.308/h | 3 h | $1.74 | $3.92 |
| NAT gateway, hourly | $0.059/h | 5 h | $0.30 | $0.30 |
| NAT gateway, data | $0.059/GB | images and model weights, 1 boot: ~6 GB (cpu), ~20 GB (gpu) | $0.35 | $1.18 |
| Fargate 0.5 vCPU / 1 GB | $0.0296/task-h ($0.04856/vCPU-h + $0.00532/GB-h) | ~25 task-h | $0.74 | $0.74 |
| CloudWatch (EMF metrics, Container Insights, logs) | $0.30/metric-month, prorated hourly | ~30 metrics, under 1 GB logs | ~$1.00 | ~$1.00 |
| ALB + internal NLB | $0.0252/h each + LCU | 5 h | ~$0.35 | ~$0.35 |
| Public IPv4 (NAT EIP + 2 ALB) | $0.005/h each | 5 h | $0.08 | $0.08 |
| EBS gp3 (host root: 40 GB cpu, 100 GB gpu) | $0.096/GB-month | 3 h | $0.02 | $0.04 |
| DynamoDB on-demand, ECR, data out, Budgets | | tiny | ~$0.05 | ~$0.05 |
| **Subtotal** | | | **~$4.65** | **~$7.70** |
| **With 30% contingency** | | | **~$6.05** | **~$10** |

The model host and the NAT data dominate. Each extra host hour costs about $0.62 (cpu) or $1.40 (gpu), counting EBS and NAT. The guards are:
- destroying the stack after every session;
- the dead-man switch on the model host (`model_host_max_hours`, default 3);
- an AWS Budget alerting at $3, $5 and $7 actual and at $9 forecast;
- the organization's Budgets spend-limit SCP.

Actual spend by service: `uv run python -m cost.measured --start 2026-10-08 --end 2026-10-11`.

## Design decisions, in short

1. **Model tier is EC2 with docker compose**, not ECS-on-EC2 or SageMaker (the `cpu` tier keeps the same shape; see [`docs/cpu-model-tier.md`](cpu-model-tier.md)). Only one GPU fits the quota and three model servers must share it; ECS gives a GPU to one container exclusively, and SageMaker wants one GPU instance per endpoint. An ASG (min = max = 1) keeps the host self-healing and an internal NLB gives it a stable address.
2. **Fault injection is a proxy mode of the Round 1 mock** (`voice-agent chaos-proxy`). It applies the same 20% failure mix and forwards healthy requests to vLLM and Speaches by path. The service talks to it through the `openai` adapters, which is the Round 1 swap story.
3. **DynamoDB replaces SQLite**, because Fargate disks are per-task and a scale-in would lose dead letters. One table holds turns and dead letters; `TransactWriteItems` keeps "turn degraded plus dead letter written" atomic. SQS was rejected for the system of record because a send cannot join that transaction. Both store backends pass the same contract tests.
4. **Autoscaling is on ActiveCalls per task** (target 10 on the GPU tier, 2 on the CPU tier; min 2, max 10), emitted as a CloudWatch EMF metric; ALB stickiness keeps each call on one task. A scheduled action pre-warms the floor before a known campaign window; the target-tracking policy does the scaling to the peak.
5. **Observability is EMF plus ALB and Container Insights metrics**, with one dashboard (traffic, capacity, dependencies) and alarms for p99 latency over 3x baseline, 5xx (target and ALB-generated), open breakers and DLQ growth. The EMF flush interval is 10 s (`EMF_INTERVAL_S`), deliberately short so the dashboard and the ActiveCalls scaling signal react within a minute, at the cost of a few more metric datapoints.
6. **IAM uses custom policies with explicit actions only.** `scripts/check_iam.py` fails on any `*` action or AWS-managed policy attachment; the checker has its own tests in `tests/scripts`.
7. **Cost guardrails:** the budget is a one-time CLI step (above, not in the Terraform); the model host dead-man switch is in the IaC, and `teardown_check.py` proves the destroy.
8. **State and secrets:** Terraform state is local and git-ignored (an S3 backend with a lockfile is the team setup); there are no secrets, as model servers need no keys and the deployer uses its own CLI profile.
