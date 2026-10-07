# Demo shot list (5 minutes)

The recording is edited down from one real run. The full campaign is 35 minutes (10 min baseline, 15 min spike, 10 min cool-down), so cut or speed up the waiting and keep the transitions. The numbers on screen must be real: do not narrate figures you have not read off the dashboard.

## Before you record

Bring the stack up and warm it first. These are not filmed except the `terraform apply` output in the first shot (record or re-run it from a short clip).

```bash
export AWS_PROFILE=predixion
# terraform.tfvars: model_tier = "gpu", allowed_cidrs = ["<your-ip>/32"], campaign_prewarm for the spike
terraform -chdir=infra apply
URL=$(terraform -chdir=infra output -raw url)
terraform -chdir=infra output dashboard_url
curl -s $URL/health                          # all breakers closed, dead_letters empty
uv run python scripts/chaos.py reset         # clean proxy counters
```

Open the dashboard in one window and a terminal in another. Set the dashboard to a 1-hour range and 10 s refresh.

## Shots

| Time | Shot | What to say / point at |
|---|---|---|
| 0:00-0:30 | Architecture slide (the README diagram), then the tail of `terraform apply` output | One apply builds everything: network, ECS, ALB, DynamoDB, the GPU host, dashboard and alarms. Show the outputs: `url`, `dashboard_url` |
| 0:30-1:15 | Dashboard at baseline | Flat 2 tasks, "Active calls" about 5 per task (target 10), p50/p90/p99 flat, HTTP 5xx 0, a steady 20% of provider attempts failing but few degraded turns (retries absorb them) |
| 1:15-2:30 | Spike phase | "Active calls" climbs above target, "Tasks: desired vs running" steps up (desired first, then running), latency percentiles hold. Show one row of "Autoscaling and task events" |
| 2:30-3:30 | LLM outage | Breaker widget goes to 2 for `llm`, "Error rate (%)" degraded % rises while HTTP 5xx stays 0, "Dead-letter queue" pending climbs |
| 3:30-4:15 | Recovery and replay | Breaker widget back to 0; replay; pending drops and "resolved by replay" rises |
| 4:15-5:00 | Campaign summary, destroy, teardown check | Summary table (`http_fail` is 0 in every phase), `terraform destroy`, then the CLEAN line of the teardown log |

## Commands for each shot

Run the campaign in terminal 1 for the whole recording. It prints one progress line every 30 seconds and the per-phase table at the end.

```bash
mkdir -p results
uv run voice-agent campaign --url $URL --json-out results/campaign.json | tee results/campaign.txt
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
curl -s $URL/health                          # llm breaker "open", dead_letters pending rising
uv run python scripts/chaos.py stats         # LLM requests flat while the breaker is open
```

**3:30-4:15 (recovery).** The breakers close by themselves once the outage window ends. Check, then replay:

```bash
curl -s $URL/health                          # every breaker "closed"
curl -s -X POST "$URL/v1/dlq/replay?limit=1000"
curl -s "$URL/v1/dlq?status=pending&limit=5" # shrinking; run the replay again for stragglers
```

While a breaker is still open the replay marks every entry `skipped` and changes nothing; wait a few seconds and repeat. Entries that fail go back to `pending` and are picked up by the next replay.

**4:15-5:00 (end).** When the campaign finishes, show its summary table (also in `results/campaign.txt`). Then:

```bash
terraform -chdir=infra destroy               # Docker Desktop must be running
uv run python scripts/teardown_check.py      # exits 0 and writes teardown/teardown-check-<UTC>.log
```

Show the CLEAN result and the log file name. Commit the log: `git add teardown/*.log`.

## Checklist while recording

- Use the dashboard from `dashboard_url`, not screenshots.
- Keep the 20% injected failure rate on throughout; it is part of the story.
- Do not leave the stack running after the take. The GPU dead-man switch stops the GPU after `gpu_max_hours` (default 4) but not the NAT gateway or load balancers.
