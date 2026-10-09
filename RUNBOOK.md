# Runbook: Collections Voice-Agent Inference Service

Scope: the Round 2 deployment in ap-southeast-2 (Sydney; see the README for why not ap-south-1) (see the README section "Round 2: AWS deployment"). Names below are the real ones from `infra/`: cluster `collectionsinference`, ECS service `voice-agent`, SNS topic `collectionsinference-alarms`, CloudWatch dashboard `collectionsinference`.

Set up your shell once (from the repo root, with the `predixion` profile configured for ap-southeast-2). The commands in this runbook assume Git Bash (or another POSIX shell):

```bash
export AWS_PROFILE=predixion AWS_REGION=ap-southeast-2
URL=$(terraform -chdir=infra output -raw url)
terraform -chdir=infra output dashboard_url      # open this in the browser
```

In PowerShell the equivalent is:

```powershell
$env:AWS_PROFILE = "predixion"; $env:AWS_REGION = "ap-southeast-2"
$URL = terraform -chdir=infra output -raw url
terraform -chdir=infra output dashboard_url
```

Use `$URL` the same way in the `curl` commands (in Windows PowerShell 5.1 call `curl.exe`, since `curl` is an alias for `Invoke-WebRequest`).

## Trigger

The alarm `collectionsinference-p99-latency-3x-baseline` fires when the p99 of the `TurnLatency` metric is above 3 x `baseline_p99_ms` for 3 of 3 consecutive 1-minute periods. With the default `baseline_p99_ms = 3000` the threshold is 9000 ms. Set `baseline_p99_ms` in `terraform.tfvars` to the p99 you measured at baseline; the dashboard draws the same line on "Turn latency percentiles (ms)".

It pages through the SNS topic `collectionsinference-alarms` (alarm and OK transitions). If `alert_email` is set, that address is subscribed, and AWS sends a confirmation email that must be accepted once or nothing is delivered. Other alarms on the same topic, which usually explain this one:

| Alarm | Meaning |
|---|---|
| `collectionsinference-breaker-open-stt` / `-llm` / `-tts` | A circuit breaker is open on at least one task: that dependency is failing |
| `collectionsinference-dlq-pending` | More than 50 dead letters pending for 5 minutes |
| `collectionsinference-http-5xx` | The service itself returned 5xx. Degraded turns are HTTP 200s, so this should stay at 0 |
| `collectionsinference-elb-5xx` | The ALB itself returned 5xx (no healthy targets, or a request cut off). A long bulk replay can cause this: see Post-incident |

## First 5 minutes

Open the dashboard (`dashboard_url`). Work through three checks, each with a dashboard widget and a CLI fallback. Do not change anything yet.

**1. Is capacity keeping up with demand?**

- Widgets: "Active calls (scaling signal)" (per-task average against the target of 10) and "Tasks: desired vs running (autoscaling)".
- Desired above running means tasks are starting; running below desired for many minutes means they cannot start (see "If autoscaling is the cause").

```bash
aws ecs describe-services --cluster collectionsinference --services voice-agent --profile predixion --query 'services[0].[desiredCount,runningCount,pendingCount]'
aws application-autoscaling describe-scaling-activities --service-namespace ecs --resource-id service/collectionsinference/voice-agent --profile predixion --max-items 10
```

The dashboard's "Autoscaling and task events" table shows the same history, plus task stops and deployments.

**2. Which breakers are open, and are we degrading?**

- Widgets: "Circuit breakers (0 closed, 1 half-open, 2 open)", "Error rate (%)" (degraded turns % versus HTTP 5xx %), "Provider failures by stage (attempts)".
- CLI fallback: `/health` reports liveness and each stage's breaker state. It is answered by one task, which is enough to see the pattern (breakers are per task). It deliberately never touches DynamoDB or the models, so a store or provider brownout cannot fail the ALB health check. The dead-letter counts are on `GET /v1/dlq` (see below).

```bash
curl -s $URL/health
```

**3. What is the model tier actually receiving?**

- Widget: "Model tier health (NLB healthy hosts)". 0 means the model host is down or still booting (a fresh boot takes about 10 minutes on the cpu tier, 15-20 on the gpu tier, to download images and weights).
- `scripts/chaos.py` runs over SSM against the running model host (`model_tier = "gpu"` or `"cpu"`; it exits with "no running model host" otherwise). It prints the chaos proxy's own per-stage request and outcome counts (what the model tier actually received). Compare them with what the service thinks it sent.

```bash
uv run python scripts/chaos.py stats
```

If `chaos.py stats` shows an outage or a failure rate you did not set, clear it:

```bash
uv run python scripts/chaos.py reset
```

Then go to the section that matches what you found.

## If downstream is the cause

Symptoms: one or more breakers at 2, "Provider failures by stage" rising, degraded turns % rising, p99 up, HTTP 5xx still 0.

1. **Confirm the breaker is doing its job.** It opens when the failure rate over its last 50 counted calls is at least 60% (minimum 20 calls in the window), then rejects calls instantly for 5 s, then lets up to 5 probes through. If a stage is clearly failing but its breaker is not open, look at the "Circuit breakers" widget per stage and at the `BREAKER_*` variables on the task (`aws ecs describe-task-definition --task-definition voice-agent --profile predixion`). The defaults are `BREAKER_WINDOW=50`, `BREAKER_MINIMUM_CALLS=20` and `BREAKER_FAILURE_THRESHOLD=0.6`, and Terraform sets none of them. The injected 20% baseline failure rate must never trip it.
2. **Every degraded turn has a dead letter by construction.** The turn is marked degraded and its dead letter is written in one DynamoDB `TransactWriteItems`, before the caller hears the fallback prompt. So callers were not dropped, and nothing needs to be reconstructed from logs. If the dead-letter count is lower than the degraded count, that is a bug: escalate.
3. **Look at the queue.**
   - Dashboard widget "Dead-letter queue" (pending, new per minute, resolved by replay).
   - HTTP: `curl -s "$URL/v1/dlq?status=pending&limit=20"`.
   - CLI, reading DynamoDB directly from your laptop:

     ```bash
     STORE_BACKEND=dynamodb AWS_PROFILE=predixion uv run voice-agent dlq list --status pending
     STORE_BACKEND=dynamodb AWS_PROFILE=predixion uv run voice-agent dlq show <id>
     ```
4. **Fix or wait out the dependency.** For the gpu or cpu tier: check the NLB health widget, then the model host's log group `/models/collectionsinference` in CloudWatch Logs (docker compose output: vLLM or llama.cpp, Speaches, chaos proxy). If the host was replaced it is reloading models (about 10 minutes on cpu, 15-20 on gpu); the breakers stay open until it is healthy, which is correct.

   If the host never becomes healthy, the bootstrap itself may have failed. Its output goes to `/var/log/collections-bootstrap.log` on the host (and is not in CloudWatch until the compose containers start). Find the instance, then read the EC2 console output or tail the log over SSM:

   ```bash
   ID=$(aws autoscaling describe-auto-scaling-groups --auto-scaling-group-names $(terraform -chdir=infra output -raw model_host_asg) --query 'AutoScalingGroups[0].Instances[0].InstanceId' --output text --profile predixion --region ap-southeast-2)
   aws ec2 get-console-output --instance-id $ID --latest --output text --profile predixion --region ap-southeast-2
   CMD=$(aws ssm send-command --instance-ids $ID --document-name AWS-RunShellScript --parameters 'commands=["tail -n 100 /var/log/collections-bootstrap.log"]' --query Command.CommandId --output text --profile predixion --region ap-southeast-2)
   aws ssm get-command-invocation --command-id $CMD --instance-id $ID --query StandardOutputContent --output text --profile predixion --region ap-southeast-2
   ```
5. **Do not replay yet.** Wait until every breaker is closed (see Post-incident). While any breaker is open a replay does nothing useful: the bulk endpoint answers 200 with every entry `skipped`, and the single-entry endpoint answers 503.

## If autoscaling is the cause

Symptoms: "Active calls" per task well above 10 and "Tasks: desired vs running" flat or desired above running; latency up with breakers closed.

1. **Desired versus running.** Use the check 1 commands above.
   - Desired equals the maximum (`max_tasks`, default 10): the ceiling is the limit. Raise `max_tasks` in `terraform.tfvars` and apply.
   - Desired above running for several minutes: tasks cannot start. Read the reason in the "Autoscaling and task events" table, or with `aws ecs describe-services --cluster collectionsinference --services voice-agent --profile predixion --query 'services[0].events[:5]'`.
2. **Fargate quota.** A new account can have a low on-demand Fargate vCPU limit.

   ```bash
   aws service-quotas get-service-quota --service-code fargate --quota-code L-3032A538 --profile predixion --region ap-southeast-2
   ```

   Each task is 1 vCPU, so the quota must be at least `max_tasks`, and about double that during a rolling deployment.
3. **Subnet IP space.** Each task takes one IP from a private subnet. The two private /23 subnets hold about 500 IPs each, so this is not a limit at 10 tasks. It matters only if the VPC CIDR or subnet sizes were changed.
4. **Cooldowns.** The target-tracking policy `active-calls-per-task` targets an average of 10 active calls per task, with a scale-out cooldown of 60 s and a scale-in cooldown of 180 s. A new task takes about a minute to start and pass health checks (30 s grace period), so a ramp faster than that is absorbed by the existing tasks first. That is why campaign windows are pre-warmed: pre-warm the floor (3 tasks), and let the ActiveCalls target tracking scale the peak. The pre-warm is not meant to cover the whole peak.
5. **Pre-warm.** For a known campaign window, set `campaign_prewarm` in `terraform.tfvars` and apply:

   ```hcl
   campaign_prewarm = { start = "at(2026-10-08T10:00:00)", end = "at(2026-10-08T10:40:00)", min_tasks = 3 }
   ```

   Times are UTC. Start it at least 2 minutes before the window. In an incident you can raise the floor by hand: `aws application-autoscaling register-scalable-target --service-namespace ecs --resource-id service/collectionsinference/voice-agent --scalable-dimension ecs:service:DesiredCount --min-capacity 3 --max-capacity 10 --profile predixion`. The next `terraform apply` resets it (see the caveats below).

### Caveats before running `terraform apply` during an incident

- Changing app source code (`src/**/*.py`, `pyproject.toml`, `uv.lock`, the `Dockerfile`; a README edit does not count) or `failure_rate` replaces the model host, which means 10-20 minutes of image and model download with the model tier down. Do not do this during a campaign window.
- An apply during a campaign window resets the pre-warm: Terraform sets the autoscaling minimum back to `min_tasks`, so the service can scale in mid-campaign.
- The model host has a dead-man switch (`model_host_max_hours`, default 3). After it fires, the ASG is at 0 and the model tier is down. Re-applying after it fired turns the host back on (and starts the boot again).
- Before the first `model_tier = "gpu"` apply, run these two checks (neither is done by Terraform):
  - The vLLM image tag exists, or the host will boot and never serve: `docker manifest inspect vllm/vllm-openai:v0.31.0 > /dev/null`.
  - The Deep Learning AMI's root device is `/dev/xvda`, which the launch template assumes:

    ```bash
    AMI=$(aws ssm get-parameter --name /aws/service/deeplearning/ami/x86_64/base-oss-nvidia-driver-gpu-amazon-linux-2023/latest/ami-id --query Parameter.Value --output text --profile predixion --region ap-southeast-2)
    aws ec2 describe-images --image-ids $AMI --query 'Images[0].RootDeviceName' --profile predixion --region ap-southeast-2
    ```
- To extend the dead-man switch, quote the address so it works in both shells: `terraform -chdir=infra apply "-replace=aws_autoscaling_schedule.model_host_off[0]"`.

## Escalation

Fill in the placeholders when the stack is deployed, and do not leave any in a live campaign.

- Primary on-call: the deploying engineer, `<name, phone>`. Backup: `<name, phone>`.
- After 15 minutes unresolved in a live campaign window, page the platform lead: `<name, phone>`.
- After 30 minutes unresolved, pause the campaign dialer (stop new calls) and switch to human agents. Calls already in progress keep getting fallback prompts and handoffs; nothing is lost, because every degraded turn is in the dead-letter queue.

## Post-incident

1. **Confirm recovery.** All three breakers at 0 on the dashboard, p99 back under the alarm line, and `curl -s $URL/health` shows `closed` for each stage. Wait until every breaker is closed (poll `/health`, or watch the "Circuit breakers" widget) before replaying.
2. **Replay the dead letters in batches of 50.** Loop until nothing is pending:

   ```bash
   curl -s -X POST "$URL/v1/dlq/replay?limit=50"
   curl -s "$URL/v1/dlq?status=pending&limit=1"     # "counts" holds the pending total; repeat both until it is 0
   ```

   Why 50: the ALB's idle timeout is 60 s and a request that runs longer is cut with a 504 (and an ELB 5xx on the dashboard). One `limit=1000` call over a few hundred entries at 2-3 s per real-model turn takes longer than that. The replay keeps running on the server after the 504, but you lose its result. Batches of 50 stay well under the limit.

   Replay is canary-first: it replays one entry, and continues (8 at a time) only after one has resolved and every breaker is closed. While any breaker is open it replays nothing: the bulk call returns HTTP 200 with every entry marked `skipped` (the single-entry endpoint `POST /v1/dlq/{id}/replay` returns 503), and no entry is charged a replay. It is safe to repeat: entries that fail go back to `pending` with their replay count incremented. Use the HTTP endpoint rather than `voice-agent dlq replay`, because the CLI on your laptop cannot reach the internal model tier. Watch the "Dead-letter queue" widget (resolved by replay) as well.
3. **Record actual versus expected.**

   | Measure | Expected | Actual |
   |---|---|---|
   | Time from first degraded turn to alarm | under 3 min | |
   | Time from alarm to mitigation | under 15 min | |
   | HTTP 5xx during the incident | 0 | |
   | Degraded turns versus dead letters written | equal | |
   | Dead letters replayed / still pending | all resolved | |
   | Peak tasks, peak ActiveCalls per task | at most 10 per task | |
   | p99 `TurnLatency` at peak | under the 3x baseline line | |

4. **Write down the cause and the one change that would have prevented it** (alarm threshold, quota, pre-warm, timeout).
5. **Teardown reminder.** If this was a demo or test stack, tear it down. The model host alone costs $0.580 per hour (cpu, m7a.2xlarge) or $1.308 per hour (gpu, g5.xlarge) on-demand in ap-southeast-2 and keeps running until you do:

   ```bash
   terraform -chdir=infra destroy           # Docker Desktop must be running
   uv run python scripts/teardown_check.py  # writes teardown/teardown-check-<UTC>.log
   ```

   The check exits 0 only when nothing billable is left. Commit the log. If the only leftover is a Container Insights log group (`/aws/ecs/containerinsights/...`), it was recreated by a late flush after the destroy: wait 5 minutes, run `aws logs delete-log-group --log-group-name <name> --profile predixion --region ap-southeast-2`, and re-run the check.
