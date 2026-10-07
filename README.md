# Resilient collections voice agent

A collections voice-agent inference service. Each caller turn goes through **STT → LLM → TTS**, and a resilience layer around the pipeline keeps a flaky provider from dropping the call:
- **Retries** with exponential backoff and full jitter.
- **A circuit breaker for each stage.**
- **Graceful degradation** to a pre-rendered fallback prompt.
- **A dead-letter queue** that lives in SQLite and can be inspected and replayed. Every failed turn is written to it *before* the caller hears the fallback.

**Round 2 (deploying this to AWS, ap-south-1): see [Round 2: AWS deployment](#round-2-aws-deployment-ap-south-1).** The rest of this README, up to that section, describes the Round 1 service that Round 2 builds on.

A local mock provider stands in for STT/LLM/TTS. It adds realistic latency, fails 20% of requests, and can force outages on demand. The provider boundary is model-agnostic: open-weight models (faster-whisper, Ollama/vLLM/llama.cpp, Kokoro) replace the mock through configuration alone.

| Brief | Where |
|---|---|
| Resilience layer: retries, backoff, circuit breaking | [`resilience/`](src/voice_agent/resilience), [§ Failure handling](#failure-handling) |
| Dead letters that can be inspected and replayed | [`store.py`](src/voice_agent/store.py), [`dlq.py`](src/voice_agent/dlq.py), [§ Dead-letter queue](#dead-letter-queue) |
| Model-agnostic dependency boundary | [`providers/`](src/voice_agent/providers), plus [`docker-compose.openweight.yml`](docker-compose.openweight.yml) for the open-weight stack |
| Half-page cost write-up **and** one paragraph on swapping the mock for an open-weight model | Submitted separately as a one-page PDF. Its cost figures are computed by [`cost/fargate_cost.py`](cost/fargate_cost.py) (`uv run python -m cost.fargate_cost`); [`docs/writeup_pdf.py`](docs/writeup_pdf.py) rebuilds it with `uv run --with reportlab python docs/writeup_pdf.py` |
| No hardcoded secrets | Keys come only from env (`SecretStr`, no defaults); [`.env.example`](.env.example) is blank |
| Runs from documented setup | [§ Quick start](#quick-start): `uv sync`, then 3 commands; or `docker compose up --build` |
| Design spec | [`docs/superpowers/specs/…-design.md`](docs/superpowers/specs/2026-10-05-resilient-voice-agent-design.md) |
| **Round 2:** AWS deployment, autoscaling, dashboard, teardown | [§ Round 2: AWS deployment (ap-south-1)](#round-2-aws-deployment-ap-south-1), [`infra/`](infra) |
| **Round 2:** runbook, demo shot list, ap-south-1 notes | [`RUNBOOK.md`](RUNBOOK.md), [`docs/demo-shot-list.md`](docs/demo-shot-list.md), [`docs/ap-south-1-notes.md`](docs/ap-south-1-notes.md) |

## Quick start

You need Python ≥ 3.11 and [uv](https://docs.astral.sh/uv/). No AWS account, API keys or GPU are needed.

```bash
uv sync                                   # install
uv run pytest -q                          # 264 tests, ~40 s

# terminal 1: mock STT/LLM/TTS provider, 20% failure rate
uv run voice-agent mock --port 9000
# terminal 2: the service
uv run voice-agent serve                  # http://127.0.0.1:8080, docs at /docs
# terminal 3: traffic
uv run voice-agent loadtest               # 50 calls x 4 turns, compares both sides
uv run voice-agent chaos-demo             # baseline -> LLM outage -> recovery -> replay
```

With Docker instead: `docker compose up --build`, then run `loadtest` / `chaos-demo` from the host. To use the DLQ CLI inside the container, run `docker compose exec agent voice-agent dlq list`.

One turn by hand:

```bash
curl -s localhost:8080/v1/calls/call-1/turns -H 'content-type: application/json' \
  -d '{"turn_id": "t1", "audio_b64": "aGVsbG8="}'
```

<details><summary>Output from one local run (Windows laptop, real processes)</summary>

`loadtest`:

```
turns sent:          240
  completed:         237
  degraded:          3  (handoff: 0)
  HTTP failures:     0   <- should always be 0
degradation causes:  stt:server_error=1, stt:rate_limited=1, tts:rate_limited=1
breakers:            stt=closed, llm=closed, tts=closed
dead letters:        pending=3, replaying=0, resolved=0
provider received (requests / ok / failed):
  stt:   303 /   238 /    65
  llm:   293 /   238 /    55
  tts:   288 /   237 /    51
```

`chaos-demo` (excerpt). LLM traffic stays flat while its breaker is open (90 → 90 → 93, and the extra 3 are half-open probes), and every degraded turn is dead-lettered and later replayed:

```
  [outage t+3s ] breakers: stt=closed llm=open   tts=closed  provider requests: stt=85  llm=90  tts=69  dlq pending=16
  [outage t+6s ] breakers: stt=closed llm=open   tts=closed  provider requests: stt=99  llm=90  tts=69  dlq pending=26
  [outage t+9s ] breakers: stt=closed llm=open   tts=closed  provider requests: stt=112 llm=93  tts=69  dlq pending=36
  [recovery t+7s] breakers: stt=closed llm=closed tts=closed provider requests: stt=129 llm=101 tts=75  dlq pending=46
4) Replay the dead-letter queue now that the provider is healthy.
   replay results: {'resolved': 43, 'failed': 3}
```

The 3 failed replays hit the mock's normal 20% failure rate on every retry. They remain `pending` and can be replayed again.
</details>

## Architecture

```
POST /v1/calls/{call_id}/turns
  └─ TurnPipeline  (per-turn deadline, default 6 s)
       ├─ write-ahead: turn row = in_progress            ── SQLite (WAL, synchronous=FULL)
       ├─ ResilientStage("stt") = Retry(Breaker(Timeout(SttProvider.transcribe)))
       ├─ ResilientStage("llm") = Retry(Breaker(Timeout(LlmProvider.complete)))
       ├─ ResilientStage("tts") = Retry(Breaker(Timeout(TtsProvider.synthesize)))
       ├─ success → turn = completed, reply audio returned
       └─ failure → ONE transaction: turn = degraded + dead_letters row
                    → return pre-rendered fallback audio + action (retry_prompt | handoff)

providers/  base.py (Protocols + ProviderError)  ← the only thing resilience/ and pipeline know
            mock.py | openai_compat.py           ← adapters; factory.py picks one per stage
```

| Module | Responsibility |
|---|---|
| `providers/base.py` | `SttProvider` / `LlmProvider` / `TtsProvider` Protocols, plus `ProviderError(kind)`, the error taxonomy |
| `providers/http.py` | The only place that maps HTTP and transport failures to error kinds |
| `resilience/retry.py`, `breaker.py`, `stage.py` | Retry policy, circuit breaker, and the composition of the two. No HTTP, no adapter imports |
| `pipeline.py` | One turn end to end: degradation, handoff decision, call history for the LLM |
| `store.py`, `dlq.py` | Audit log, dead letters, crash recovery, replay |
| `api.py`, `cli.py`, `wiring.py` | HTTP API, operator CLI, and the single wiring that API, CLI and tests share |
| `mock_provider/app.py` | The mock "provider having a bad day" |

## Failure handling

**Error taxonomy.** Adapters turn every failure into a `ProviderError` with a `kind`:

| Kind | Source | Retried | Counts toward breaker |
|---|---|:-:|:-:|
| `timeout`, `connection` | attempt timeout, connect/read errors, HTTP 408 | ✓ | ✓ |
| `rate_limited` | 429 (honours `Retry-After`) | ✓ | ✓ |
| `server_error` | 5xx | ✓ | ✓ |
| `malformed` | a 2xx with an unparseable or wrong-shaped body | ✓ | ✓ |
| `client_error`, `auth` | 400/404/422, 401/403 | ✗ | ✗ |

A 4xx means *our* request or configuration is wrong. The provider itself is healthy, so a 4xx never trips the breaker. Bugs in our own code are not `ProviderError`s, so they are never retried, but they are still dead-lettered.

**Retry** ([`retry.py`](src/voice_agent/resilience/retry.py)):
- Up to 3 attempts. The delay is `uniform(0, min(2 s, 0.1 s · 2ⁿ))` (full jitter), so many calls failing together don't retry in lock-step.
- `Retry-After` is honoured, capped at the maximum delay.
- Retries stay inside the turn deadline: no backoff sleeps past it, no attempt starts with less than 50 ms left, and each attempt's timeout is `min(stage timeout, remaining budget)`. A hung provider costs at most one attempt timeout.

**Circuit breaker** ([`breaker.py`](src/voice_agent/resilience/breaker.py)), one per stage:
- **Opening:** it looks at a rolling window of the last 50 counted calls and opens when a *failure* brings the failure rate to ≥ 60% (with at least 20 calls in the window).
- **Why those thresholds:** the baseline 20% failure rate must not trip it. I simulated the breaker class itself rather than relying on back-of-envelope binomials:
  - At 50% the breaker false-tripped in 0.5% of 3,000-attempt runs, mostly during the 20-call warm-up.
  - At 60% the chance of a false trip drops to about 1 in 10,000 per breaker warm-up, at service start-up or after a recovery (analytically P(≥ 12 of 20) ≈ 1.0e-4; simulated 8.8e-5). In steady state it never tripped across about 30 million attempts.
  - At 60% a real outage still trips it within 27 attempts, about 9 turns.
- **While open:** for 5 s, calls are rejected immediately with `circuit_open`. Nothing is sent to the provider and the caller sees no added latency. A rejection is never retried.
- **Half-open:** up to 5 probe calls are let through, and a majority decides. Mostly successes close the circuit with an empty window; mostly failures reopen it with a fresh cool-down. A single flaky probe can't cause flapping.
- **Stale results:** each permit carries the breaker's epoch, so a slow call admitted before a state change can't be miscounted as a probe.
- **Composition:** the order is `Retry(Breaker(attempt))`. Every attempt asks the breaker first, so an outage that starts mid-retry stops the retry loop.
- **Stages are independent:** a TTS outage doesn't stop STT. STT still runs during an LLM outage, so the dead letter records the caller's words.

**Degradation** ([`pipeline.py`](src/voice_agent/pipeline.py)). A turn is never dropped, and it is always recorded:
1. The turn row is written (`in_progress`) before any provider is called.
2. On failure, the turn is marked `degraded` and its dead-letter row is inserted, both in one transaction, before the response is sent. This covers provider errors, open circuits, blown deadlines, our own bugs, and cancellation (for example at shutdown). Finalization runs shielded from cancellation, so a recording that has started always finishes.
3. The caller still gets **HTTP 200 with audio**: a pre-rendered fallback prompt that doesn't depend on the TTS stage that may have just failed, plus `action`. The action is `retry_prompt`, or `handoff` (transfer to a human / schedule a callback) after 2 consecutive degraded turns on the same call.
4. A process crash mid-turn leaves an `in_progress` row behind. At the next start-up, `Store.recover()` marks those turns `interrupted` and dead-letters them.
5. While the service runs, a **lease sweeper** (every 30 s) does the same for any turn untouched for longer than `max(60 s, 3 × turn deadline)`. It also returns any `replaying` claim older than that to `pending`. So even a failed database write can't strand a record until the next restart. A failed write doesn't drop the call either: the caller still gets their audio. If even the write-ahead fails (storage down), the turn isn't processed, the caller hears the fallback, and the error is logged loudly. That is the one case with no record.
6. Every state transition is guarded in SQL (`UPDATE … WHERE status = <expected>`), so a stale or concurrent writer can never skip or reverse a state.

Duplicate turn ids (a telephony retry) never re-run the providers. They return `409` with the original outcome:
- **Completed turns:** the reply text. Audio isn't stored, so it isn't returned.
- **Degraded or in-progress turns:** the fallback prompt, both text and audio, for the stored `action`.

`/health` never depends on provider health. That way a provider outage can't make the load balancer recycle orchestrator tasks that are degrading correctly. It still reports breaker states and DLQ counts. `/metrics` exposes Prometheus counters for attempts, retries, breaker state and transitions, turns, dead letters and replays.

## Dead-letter queue

A dead letter is a row in the `dead_letters` table of `data/voice_agent.db`. It holds:
- the original request (the audio),
- the failed stage and error kind,
- **every attempt** (stage, attempt number, outcome, error, HTTP status, duration, backoff),
- partial results (for example, the transcript when only the LLM failed),
- replay bookkeeping.

```bash
uv run voice-agent dlq list [--status pending] [--json]   # table of entries + counts
uv run voice-agent dlq show 7                             # full entry incl. attempts
uv run voice-agent dlq replay 7 9                         # replay specific entries
uv run voice-agent dlq replay --all                       # replay every pending entry
sqlite3 data/voice_agent.db 'select id, failed_stage, error_kind, status from dead_letters'
```

The same operations are available over HTTP: `GET /v1/dlq`, `GET /v1/dlq/{id}`, `POST /v1/dlq/{id}/replay`, and `POST /v1/dlq/replay`. `GET /v1/calls/{call_id}` returns a call's full turn-by-turn audit trail.

**Replay semantics** ([`dlq.py`](src/voice_agent/dlq.py)):
- **Claiming:** an entry is claimed atomically (`pending → replaying`), so two operators can never replay it at the same time.
- **Running:** the stored turn re-runs through the *same* resilient pipeline and the same breakers. A provider that is still down fails fast instead of being hammered.
- **Success:** the turn becomes `completed_on_replay`, with the transcript and reply recorded. The entry is marked `resolved`.
- **Failure:** the entry returns to `pending`, with `replay_count + 1` and the error recorded.
- **Open circuit:** while any circuit is open, a replay is `skipped` (HTTP 503) without being claimed or charged a `replay_count`. It would only fail fast.
- **Bulk replay:** it replays one entry at a time until one has *resolved* and every circuit is closed. That first success is the canary, needed because a fresh CLI process's breakers know nothing of an ongoing outage. If a replay is deferred or fails for any reason other than its own unusable payload, it stops and marks the rest `skipped` (untouched). After that it runs 8 entries at a time.
- **CLI safety:** the CLI's `dlq replay` is safe to run while the service is running. It never runs recovery, so it can't reclaim the service's in-flight turns, and atomic claims prevent double replays.
- **Purpose:** the live moment has passed, so a replay *completes the compliance record* (what the debtor said, what the agent would have said) and can drive a callback. It doesn't speak to the caller.

## Configuration

Everything is set through environment variables (or `.env`). See [`.env.example`](.env.example) for the full list and its defaults. There are no secrets in the repo: API keys (`*_API_KEY`) are optional, have no default, and are held as `SecretStr` so they never appear in logs or reprs. A key is sent as `Authorization: Bearer` only when it is set. The mock reads `MOCK_FAILURE_RATE`, `MOCK_SEED`, `MOCK_LATENCY_SCALE` and `MOCK_HANG_S` from the process environment.

**Pointing at a different mock:**
- If your mock serves this JSON shape (`POST {base}/stt|llm|tts`), set `*_BASE_URL`.
- If it speaks the OpenAI-compatible API, set `*_PROVIDER=openai`.
- Otherwise, add an adapter in `providers/`. Nothing else changes.

The mock's `POST /admin/chaos {"stage": "llm", "outage_s": 30}`, `{"failure_rate": 0.5}`, `GET /admin/stats` and `POST /admin/reset` drive the scenarios shown above.

## Tests

`uv run pytest -q` runs 264 tests in about 40 s. CI also runs `ruff`, `ruff format --check`, `mypy --strict`, a Docker build and `terraform validate` on `infra/`.

- **Unit tests:** retry jitter bounds and determinism; every breaker transition, including stale-epoch outcomes and probe limits; and the stage composition, all with a fake clock.
- **Adapter contract tests:** run with `httpx.MockTransport`.
- **Store and DLQ tests:** atomicity, exclusive claims, crash recovery, replay backpressure.
- **End-to-end tests** ([`tests/test_e2e.py`](tests/test_e2e.py)): the real wiring runs against the real mock app in-process, and the assertions are made from the **provider's side**:
  - At 20% failure, every turn ends `completed` or `degraded` with a dead letter, and the provider sees retries.
  - During a forced outage, the provider's request count stays flat while the breaker is open.
  - The breaker half-opens and closes after recovery, and dead letters replay cleanly.
  - A hung provider is cut off by the deadline.

## Production notes and next steps

- **Distributed state:** breaker state is per process, which suits Fargate tasks that each protect themselves. The dead-letter store would become a managed queue plus a table (SQS + DynamoDB, or Postgres) instead of a local SQLite file.
- **Data protection:** transcripts and audio in collections are sensitive. They need encryption at rest, a retention policy, and access audit on the DLQ.
- **Schema changes** are versioned (`PRAGMA user_version`). Older databases, such as a `data/` directory or the `agent-data` Docker volume from an earlier build, are migrated in place at start-up.
- **Authentication:** the API (including the DLQ endpoints, which expose audio and transcripts) has no auth, a stated non-goal for this local exercise. In production it would sit behind service-to-service auth.
- **Streaming:** real calls stream audio (WebSocket/RTP) with partial transcripts. The turn-level boundary used here stays the same, and the stage timeouts become time-to-first-token budgets.
- **Hedging:** hedged requests or a fallback provider per stage (for example, a smaller LLM when the primary's breaker is open) would let more turns complete instead of degrading.

---

## Round 2: AWS deployment (ap-south-1)

The Round 1 service, deployed to a fresh AWS account in ap-south-1 as a production-shaped system. One `terraform apply` builds everything (nothing from the console); it runs a scripted campaign against real open-weight STT/LLM/TTS models on one GPU instance with Round 1's 20% fault injection in front of them, autoscales on in-flight calls, is observable from one CloudWatch dashboard, and is torn down with `terraform destroy` plus a log proving nothing billable remains.

Other documents: [`RUNBOOK.md`](RUNBOOK.md) (what to do when the latency alarm fires), [`docs/demo-shot-list.md`](docs/demo-shot-list.md) (5-minute recording plan with commands), [`docs/ap-south-1-notes.md`](docs/ap-south-1-notes.md) (data residency, latency, AZs, DR), and the design spec [`docs/superpowers/specs/2026-10-07-round2-aws-design.md`](docs/superpowers/specs/2026-10-07-round2-aws-design.md).

### Architecture

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

With `model_tier = "mock"` the internal NLB fronts the Round 1 mock provider on Fargate instead of the GPU host. That is a cheap way to check all the infrastructure before paying for a GPU.

### Prerequisites

- An AWS account on the Free plan (this one has $100 of credit).
- If the account sits in an AWS Organization, the organization's SCP must allow EC2, ECS, ELB, DynamoDB, CloudWatch, Logs, ECR, SSM and Auto Scaling in ap-south-1. Otherwise `apply` fails with access-denied errors that name no policy.
- A quota of at least 4 vCPUs for "Running On-Demand G and VT instances" (quota code `L-DB2E81BA`). New accounts start at 0. Request it on day one, because approval can take a day or more:

  ```bash
  aws service-quotas get-service-quota --service-code ec2 --quota-code L-DB2E81BA --profile predixion
  aws service-quotas request-service-quota-increase --service-code ec2 --quota-code L-DB2E81BA --desired-value 4 --profile predixion
  ```
- Docker Desktop, running, for every `terraform` command including `destroy`. Terraform builds and pushes the app image through the docker provider (`docker_host` defaults to the Windows named pipe; on Linux or macOS set `docker_host = "unix:///var/run/docker.sock"`).
- Terraform 1.9 or later, AWS CLI v2, and [uv](https://docs.astral.sh/uv/).

### One-time setup

**1. The `predixion` profile.** All commands use a profile of that name, scoped to this project:

```bash
aws configure --profile predixion          # access key, secret, default region ap-south-1, output json
aws sts get-caller-identity --profile predixion
```

**2. A budget that cannot be fooled by the credit.** The credit would hide real spend from a normal budget, so this one is a $100 gross-spend budget (`IncludeCredit` is false) with alerts at 10, 25, 50 and 80% of actual spend and at 100% of forecast. It was created once with the command below (the file contents are shown as a shape: the name is your choice, and the email address and account id are placeholders) (Budgets is a global service served from us-east-1):

```bash
aws budgets create-budget --account-id <ACCOUNT_ID> --budget file://budget.json --notifications-with-subscribers file://notifs.json --profile predixion --region us-east-1
```

`budget.json`:

```json
{
  "BudgetName": "predixion-gross-spend",
  "BudgetType": "COST",
  "TimeUnit": "MONTHLY",
  "BudgetLimit": { "Amount": "100", "Unit": "USD" },
  "CostTypes": {
    "IncludeTax": true, "IncludeSubscription": true, "IncludeSupport": true,
    "IncludeRefund": false, "IncludeCredit": false, "IncludeUpfront": true,
    "IncludeRecurring": true, "IncludeOtherSubscription": true,
    "IncludeDiscount": true, "UseAmortized": false, "UseBlended": false
  }
}
```

`notifs.json` has one entry per threshold (the others are the same with 25, 50 and 80, and a final one with `"NotificationType": "FORECASTED"` and `"Threshold": 100`):

```json
[
  {
    "Notification": {
      "NotificationType": "ACTUAL",
      "ComparisonOperator": "GREATER_THAN",
      "Threshold": 10,
      "ThresholdType": "PERCENTAGE"
    },
    "Subscribers": [{ "SubscriptionType": "EMAIL", "Address": "you@example.com" }]
  }
]
```

**3. `terraform.tfvars`.** Copy the example and edit it (the file is git-ignored):

```bash
cp infra/terraform.tfvars.example infra/terraform.tfvars
```

Set `allowed_cidrs` to your own public IP (`curl -s https://checkip.amazonaws.com` and add `/32`): the ALB accepts traffic only from there. `alert_email` is optional; if set, accept the SNS confirmation email. The variables you are likely to change:

| Variable | Default | Meaning |
|---|---|---|
| `allowed_cidrs` | none (required) | Who can reach the ALB |
| `model_tier` | `mock` | `mock` (Fargate mock provider) or `gpu` (g5.xlarge with vLLM and Speaches) |
| `min_tasks` / `max_tasks` | 2 / 10 | Fargate task floor and ceiling |
| `target_active_calls` | 10 | Autoscaling target, active calls per task |
| `campaign_prewarm` | none | `{ start, end, min_tasks }`, `at(...)` or `cron(...)` in UTC |
| `failure_rate` | 0.2 | Injected provider failure rate (changing it replaces the GPU host) |
| `baseline_p99_ms` | 3000 | The latency alarm fires above 3x this |
| `gpu_max_hours` | 4 | Dead-man switch: the GPU scales to zero this long after creation |
| `docker_host` | Windows named pipe | Docker daemon address |

### Deploy

All Terraform commands run from the repo root through `-chdir=infra`. The sequence:

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

# 5. Switch to the GPU tier: set model_tier = "gpu" in infra/terraform.tfvars, then
docker manifest inspect vllm/vllm-openai:v0.31.0 > /dev/null      # the image tag must exist
terraform -chdir=infra apply
# The GPU host downloads about 11 GB of images and 7 GB of model weights: allow 15-20 minutes
# before the "Model tier health (NLB healthy hosts)" widget shows 1.

# 6. The campaign: 10 min at 10 calls/min, 15 min at 50 calls/min, 10 min at 10 calls/min
mkdir -p results
uv run voice-agent campaign --url $URL --json-out results/campaign.json

# 7. While it runs (during the spike): chaos, then replay
uv run python scripts/chaos.py outage --stage llm --seconds 60
curl -s -X POST "$URL/v1/dlq/replay?limit=1000"

# 8. Tear down, then prove it
terraform -chdir=infra destroy
uv run python scripts/teardown_check.py
```

Notes:
- The first `apply` also builds and pushes the app image (a few minutes) and waits for the ECS service to reach steady state.
- `chaos.py` works only with `model_tier = "gpu"` (it reaches the chaos proxy over SSM). Flags: `stats`, `reset`, `outage --stage {stt,llm,tts} --seconds N`, `rate --rate 0.2 [--stage ...]`.
- The campaign `--profile` option is the traffic profile as `name:minutes:calls_per_min,...`, not an AWS profile. Its exit code is 1 if any HTTP request failed; the target is 0 failures.
- Some policy values are only known after apply. If the check prints "resolve at apply", re-run it on the state once the stack is up: `terraform -chdir=infra show -json > infra/plan.json`, then `uv run python scripts/check_iam.py infra/plan.json`.
- Applying `tfplan` (step 3) uses the exact plan that was IAM-checked. Later applies prompt for confirmation.
- `terraform output` shows `url`, `dashboard_url`, `cluster`, `models_endpoint`, `gpu_asg`, `state_table` and `app_image`.
- `teardown_check.py` writes `teardown/teardown-check-<UTC>.log` and exits 0 only when nothing is left in the region (instances, NAT gateways, load balancers, Elastic IPs, volumes, ENIs, and the rest). Commit that log.
- Operating it, and what changes are expensive to apply, is in [`RUNBOOK.md`](RUNBOOK.md).

### Cost

Estimated, not measured. The ap-south-1 on-demand prices were checked on 2026-10-07, assuming the stack is up for about 6 hours in total (build and debug, a rehearsal, the recorded demo), the GPU runs about 5 hours and boots twice, and Fargate averages 4 tasks.

| Item | Unit price | Usage | Estimate |
|---|---|---|---|
| g5.xlarge | $1.208/h | 5 h | $6.04 |
| NAT gateway, hourly | $0.056/h | 6 h | $0.34 |
| NAT gateway, data | $0.056/GB | ~40 GB (images and model weights, 2 boots) | $2.24 |
| Fargate 1 vCPU / 2 GB | $0.05187/task-h | ~24 task-h | $1.25 |
| CloudWatch (EMF metrics, Container Insights, logs) | $0.30/metric-month, prorated hourly | ~30 metrics, under 1 GB logs | ~$1.00 |
| ALB + internal NLB | $0.0239/h each + LCU | 6 h | ~$0.40 |
| Public IPv4 (NAT EIP + 2 ALB) | $0.005/h each | 6 h | $0.09 |
| EBS gp3 (GPU root, 100 GB) | $0.0912/GB-month | 6 h | $0.08 |
| DynamoDB on-demand, ECR, data out, Budgets | | tiny / free tier | ~$0.05 |
| **Subtotal** | | | **~$11.50** |
| **With 20% contingency** | | | **~$14 (about ₹1,200)** |

The GPU and the NAT data dominate, and each extra GPU hour costs about $1.30. The two levers are destroying the stack between sessions and the GPU dead-man switch. These are estimates; measured numbers from Cost Explorer and CloudWatch will replace them after the demo run.

| | Estimate | Measured (to be filled after the demo) |
|---|---|---|
| Total | ~$11.50 (~$14 with contingency) | |
| GPU hours | 5 | |
| NAT data | ~40 GB | |

### Design decisions, in short

1. **Model tier is EC2 with docker compose**, not ECS-on-EC2 or SageMaker. Only one GPU fits the quota and three model servers must share it; ECS gives a GPU to one container exclusively, and SageMaker wants one GPU instance per endpoint. An ASG (min = max = 1) keeps the host self-healing and an internal NLB gives it a stable address.
2. **Fault injection is a proxy mode of the Round 1 mock** (`voice-agent chaos-proxy`). It applies the same 20% failure mix and forwards healthy requests to vLLM and Speaches by path. The service talks to it through the `openai` adapters, which is the Round 1 swap story.
3. **DynamoDB replaces SQLite**, because Fargate disks are per-task and a scale-in would lose dead letters. One table holds turns and dead letters; `TransactWriteItems` keeps "turn degraded plus dead letter written" atomic. SQS was rejected for the system of record because a send cannot join that transaction. Both store backends pass the same contract tests.
4. **Autoscaling is on ActiveCalls per task** (target 10, min 2, max 10), emitted as a CloudWatch EMF metric; ALB stickiness keeps each call on one task. A scheduled action pre-warms before a known campaign window.
5. **Observability is EMF plus ALB and Container Insights metrics**, with one dashboard (traffic, capacity, dependencies) and alarms for p99 latency over 3x baseline, 5xx, open breakers and DLQ growth.
6. **IAM uses custom policies with explicit actions only.** `scripts/check_iam.py` fails on any `*` action or AWS-managed policy attachment; the checker has its own tests in `tests/scripts`.
7. **Cost guardrails live in the IaC:** the budget above, a GPU dead-man switch, and `teardown_check.py`.
8. **State and secrets:** Terraform state is local and git-ignored (an S3 backend with a lockfile is the team setup); there are no secrets, as model servers need no keys and the deployer uses its own CLI profile.

### Deliverables

| Deliverable | Where |
|---|---|
| Terraform that builds and tears down everything | [`infra/`](infra); CI runs `terraform fmt -check`, `init -backend=false` and `validate` |
| Service changes for AWS (DynamoDB store, EMF metrics, call tracking, `campaign` command, chaos proxy) | [`src/voice_agent`](src/voice_agent), [`src/mock_provider`](src/mock_provider) |
| Scripts | [`scripts/chaos.py`](scripts/chaos.py), [`scripts/check_iam.py`](scripts/check_iam.py), [`scripts/teardown_check.py`](scripts/teardown_check.py), [`scripts/make_utterances.ps1`](scripts/make_utterances.ps1) |
| Runbook | [`RUNBOOK.md`](RUNBOOK.md) |
| 5-minute demo shot list | [`docs/demo-shot-list.md`](docs/demo-shot-list.md) |
| ap-south-1 notes | [`docs/ap-south-1-notes.md`](docs/ap-south-1-notes.md) |
| Cost write-up | This section holds the estimate. The one-page PDF (Round 1 cost, and the swap-the-mock paragraph) is sent separately and is not in the repo, and Round 2's measured numbers are added after the demo |
| Teardown proof | `teardown/*.log`, written by `teardown_check.py` and committed after the destroy |
| Reimbursement | The billing PDF is supplied separately by the account owner |
