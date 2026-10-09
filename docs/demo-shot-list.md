# Demo shot list (5 minutes)

The recording is edited down from one real run. The full campaign is 35 minutes (10 min baseline, 15 min spike, 10 min cool-down), so cut or speed up the waiting and keep the transitions. The numbers on screen must be real: do not narrate figures you have not read off the dashboard.

## Before you record

Bring the stack up and warm it first. These are not filmed except the `terraform apply` output in the first shot (record or re-run it from a short clip).

```bash
export AWS_PROFILE=predixion
# terraform.tfvars: model_tier = "cpu" (or "gpu" with GPU quota; see docs/cpu-tier.md), target_active_calls = 4, allowed_cidrs = ["<your-ip>/32"],
#   campaign_prewarm = { ..., min_tasks = 3 }   # pre-warm the floor; autoscaling must add the rest during the spike
terraform -chdir=infra apply
URL=$(terraform -chdir=infra output -raw url)     # PowerShell: $URL = terraform -chdir=infra output -raw url
terraform -chdir=infra output dashboard_url
curl -s $URL/health                          # all breakers closed
curl -s "$URL/v1/dlq?limit=1"                # counts: nothing pending
uv run python scripts/chaos.py reset         # clean proxy counters
```

Open the dashboard in one window and a terminal in another. Set the dashboard to a 1-hour range and 10 s refresh.

## Shots

| Time | Shot | What to say / point at |
|---|---|---|
| 0:00-0:30 | Architecture slide (the README diagram), then the tail of `terraform apply` output | One apply builds everything: network, ECS, ALB, DynamoDB, the model host, dashboard and alarms. Show the outputs: `url`, `dashboard_url` |
| 0:30-1:15 | Dashboard at baseline | Flat at the pre-warmed floor of 3 tasks (2 without the pre-warm), "Active calls" well under target (gpu: about 5 per task against 10; cpu: about 2 per task against 4), p50/p90/p99 flat, HTTP 5xx 0, a steady 20% of provider attempts failing but few degraded turns (retries absorb them) |
| 1:15-2:30 | Spike phase | "Active calls" climbs above target, and ActiveCalls target tracking adds tasks above the pre-warmed floor of 3: "Tasks: desired vs running" steps up (desired first, then running). This scale-out is the point of the shot, so the pre-warm must not already cover the peak, latency percentiles hold. Show one row of "Autoscaling and task events" |
| 2:30-3:30 | LLM outage | Breaker widget goes to 2 for `llm`, "Error rate (%)" degraded % rises while HTTP 5xx stays 0, "Dead-letter queue" pending climbs |
| 3:30-4:15 | Recovery and replay | Breaker widget back to 0; replay; pending drops and "resolved by replay" rises |
| 4:15-5:00 | Campaign summary, destroy, teardown check | Summary table (`http_fail` is 0 in every phase), `terraform destroy`, then the CLEAN line of the teardown log |

## Commands for each shot

Run the campaign in terminal 1 for the whole recording. It prints one progress line every 30 seconds and the per-phase table at the end.

```bash
mkdir -p results
# gpu tier:
uv run voice-agent campaign --url $URL --json-out results/campaign.json | tee results/campaign.txt
# cpu tier (docs/cpu-tier.md): the same shape at a CPU-sized rate
uv run voice-agent campaign --url $URL --profile baseline:10:4,spike:15:20,cooldown:10:4 --json-out results/campaign.json | tee results/campaign.txt
```

**0:00-0:30.** Slide: the diagram in the README section "Round 2: AWS deployment". Terminal: show the end of a previous apply, or run `terraform -chdir=infra output`.

**0:30-1:15 (baseline).** Nothing to type. Terminal 1 is already running the campaign. Narrate from the dashboard (`terraform -chdir=infra output dashboard_url`).

**1:15-2:30 (spike).** The campaign switches phase by itself (`phase=spike` in the progress lines). Optionally show the capacity numbers in terminal 2:

```bash
aws ecs describe-services --cluster collectionsinference --services voice-agent --profile predixion --query 'services[0].[desiredCount,runningCount,pendingCount]'
aws application-autoscaling describe-scaling-activities --service-namespace ecs --resource-id service/collectionsinference/voice-agent --profile predixion --max-items 5
```

**2:30-3:30 (outage).** Start it during the spike, 60 seconds long:

```bash
uv run python scripts/chaos.py outage --stage llm --seconds 60
curl -s $URL/health                          # llm breaker "open"
curl -s "$URL/v1/dlq?status=pending&limit=1"  # counts: pending rising
uv run python scripts/chaos.py stats         # LLM requests flat while the breaker is open
```

**3:30-4:15 (recovery).** The breakers close by themselves once the outage window ends. Check, then replay:

```bash
curl -s $URL/health                          # every breaker "closed": do not replay before this
curl -s -X POST "$URL/v1/dlq/replay?limit=50"
curl -s "$URL/v1/dlq?status=pending&limit=1" # counts: shrinking; repeat both until pending is 0
```

Replay in batches of 50 and repeat until nothing is pending: the ALB cuts a request after 60 s idle, so one large replay would time out. While a breaker is still open the bulk replay returns 200 with every entry `skipped` and changes nothing; wait a few seconds and repeat. Entries that fail go back to `pending` and are picked up by the next batch.

**4:15-5:00 (end).** When the campaign finishes, show its summary table (also in `results/campaign.txt`). Then:

```bash
terraform -chdir=infra destroy               # Docker Desktop must be running
uv run python scripts/teardown_check.py      # exits 0 and writes teardown/teardown-check-<UTC>.log
```

Show the CLEAN result and the log file name. Commit the log: `git add teardown/*.log`.

## Checklist while recording

- Use the dashboard from `dashboard_url`, not screenshots.
- Keep the 20% injected failure rate on throughout; it is part of the story.
- Do not leave the stack running after the take. The dead-man switch stops the model host after `model_host_max_hours` (default 3) but not the NAT gateway or load balancers.
