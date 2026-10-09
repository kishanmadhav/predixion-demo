# Resilient collections voice agent

A collections voice-agent inference service. Each caller turn goes through **STT → LLM → TTS**, and a resilience layer around the pipeline keeps a flaky provider from dropping the call:
- **Retries** with exponential backoff and full jitter.
- **A circuit breaker for each stage.**
- **Graceful degradation** to a pre-rendered fallback prompt.
- **A dead-letter queue** that lives in SQLite locally (DynamoDB on AWS, Round 2) and can be inspected and replayed. Every failed turn is written to it *before* the caller hears the fallback.

**Round 2 (deploying this to AWS, ap-southeast-2): see [Round 2: AWS deployment](#round-2-aws-deployment-ap-southeast-2).** The rest of this README, up to that section, describes the Round 1 service that Round 2 builds on.

A local mock provider stands in for STT/LLM/TTS. It adds realistic latency, fails 20% of requests, and can force outages on demand. The provider boundary is model-agnostic: open-weight models (faster-whisper, Ollama/vLLM/llama.cpp, Kokoro) replace the mock through configuration alone.

| Brief | Where |
|---|---|
| Resilience layer: retries, backoff, circuit breaking | [`resilience/`](src/voice_agent/resilience), [§ Failure handling](#failure-handling) |
| Dead letters that can be inspected and replayed | [`store/`](src/voice_agent/store), [`dlq.py`](src/voice_agent/dlq.py), [§ Dead-letter queue](#dead-letter-queue) |
| Model-agnostic dependency boundary | [`providers/`](src/voice_agent/providers), plus [`docker-compose.openweight.yml`](docker-compose.openweight.yml) for the open-weight stack |
| Half-page cost write-up **and** one paragraph on swapping the mock for an open-weight model | Submitted separately as a one-page PDF. Its cost figures are computed by [`cost/fargate_cost.py`](cost/fargate_cost.py) (`uv run python -m cost.fargate_cost`); [`docs/writeup_pdf.py`](docs/writeup_pdf.py) rebuilds it with `uv run --with reportlab python docs/writeup_pdf.py` |
| No hardcoded secrets | Keys come only from env (`SecretStr`, no defaults); [`.env.example`](.env.example) is blank |
| Runs from documented setup | [§ Quick start](#quick-start): `uv sync`, then 3 commands; or `docker compose up --build` |
| Design spec | [`docs/superpowers/specs/…-design.md`](docs/superpowers/specs/2026-10-05-resilient-voice-agent-design.md) |
| **Round 2:** AWS deployment, autoscaling, dashboard, teardown | [§ Round 2: AWS deployment (ap-southeast-2)](#round-2-aws-deployment-ap-southeast-2), [`infra/`](infra) |
| **Round 2:** runbook, demo shot list, region notes | [`RUNBOOK.md`](RUNBOOK.md), [`docs/demo-shot-list.md`](docs/demo-shot-list.md), [`docs/ap-south-1-notes.md`](docs/ap-south-1-notes.md) |

## Quick start

You need Python ≥ 3.11 and [uv](https://docs.astral.sh/uv/). No AWS account, API keys or GPU are needed.

```bash
uv sync                                   # install
uv run pytest -q                          # 279 tests, ~40 s

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
       ├─ write-ahead: turn row = in_progress            ── store (SQLite WAL locally; DynamoDB on AWS)
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
| `store/`, `dlq.py` | Audit log, dead letters, crash recovery, replay (SQLite locally, DynamoDB on AWS) |
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

`/health` never depends on provider or store health. That way a provider outage or a DynamoDB brownout can't make the load balancer recycle orchestrator tasks that are degrading correctly. It reports liveness and this task's breaker states; the dead-letter counts are on `GET /v1/dlq` (`curl -s "localhost:8080/v1/dlq?limit=1"` returns them under `counts`). `/metrics` exposes Prometheus counters for attempts, retries, breaker state and transitions, turns, dead letters and replays.

## Dead-letter queue

A dead letter is a row in the `dead_letters` table of `data/voice_agent.db` (with the SQLite backend; on AWS it is an item in the DynamoDB table, same fields). It holds:
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

`uv run pytest -q` runs 279 tests in about 40 s. CI also runs `ruff`, `ruff format --check`, `mypy --strict`, a Docker build and `terraform validate` on `infra/`.

- **Unit tests:** retry jitter bounds and determinism; every breaker transition, including stale-epoch outcomes and probe limits; and the stage composition, all with a fake clock.
- **Adapter contract tests:** run with `httpx.MockTransport`.
- **Store and DLQ tests:** atomicity, exclusive claims, crash recovery, replay backpressure.
- **End-to-end tests** ([`tests/test_e2e.py`](tests/test_e2e.py)): the real wiring runs against the real mock app in-process, and the assertions are made from the **provider's side**:
  - At 20% failure, every turn ends `completed` or `degraded` with a dead letter, and the provider sees retries.
  - During a forced outage, the provider's request count stays flat while the breaker is open.
  - The breaker half-opens and closes after recovery, and dead letters replay cleanly.
  - A hung provider is cut off by the deadline.

## Production notes and next steps

- **Distributed state:** breaker state is per process, which suits Fargate tasks that each protect themselves. The dead-letter store is shared across tasks in Round 2 (DynamoDB); a managed queue (SQS) in front of replay is the next step if replay volume grows.
- **Retention:** the DynamoDB table has no TTL, so turns, audio and dead letters are kept until the stack is destroyed. That suits a demo. Production needs a retention TTL (and an archive, if the compliance record must outlive it) set from policy.
- **Data protection:** transcripts and audio in collections are sensitive. They need encryption at rest, a retention policy, and access audit on the DLQ.
- **Schema changes** are versioned (`PRAGMA user_version`). Older databases, such as a `data/` directory or the `agent-data` Docker volume from an earlier build, are migrated in place at start-up.
- **Authentication:** the API (including the DLQ endpoints, which expose audio and transcripts) has no auth, a stated non-goal for this local exercise. In production it would sit behind service-to-service auth.
- **Streaming:** real calls stream audio (WebSocket/RTP) with partial transcripts. The turn-level boundary used here stays the same, and the stage timeouts become time-to-first-token budgets.
- **Hedging:** hedged requests or a fallback provider per stage (for example, a smaller LLM when the primary's breaker is open) would let more turns complete instead of degrading.

---

## Round 2: AWS deployment (ap-southeast-2)

The Round 1 service, deployed to a fresh AWS account in ap-southeast-2 as a production-shaped system. One `terraform apply` builds everything (nothing from the console); it runs a scripted campaign against real open-weight STT/LLM/TTS models on one model host with Round 1's 20% fault injection in front of them, autoscales on in-flight calls, is observable from one CloudWatch dashboard, and is torn down with `terraform destroy` plus a log proving nothing billable remains.

**Region.** The brief asks for ap-south-1 (Mumbai). The deployment account sits in an AWS Organization whose region-restriction SCP allows workloads only in ap-southeast-2 (Sydney), and it could not be edited, so the stack runs in Sydney. Nothing in the code is tied to a region: `terraform apply -var region=ap-south-1 -var 'az_ids=["aps1-az1","aps1-az3"]'` deploys to Mumbai unchanged. [`docs/ap-south-1-notes.md`](docs/ap-south-1-notes.md) explains why Mumbai is the production choice for this workload.

**Models on CPU, not GPU (approved deviation).** The design runs the models on a g5.xlarge. AWS declined the account's GPU quota (new accounts start at 0, and an appeal is pending), so the deployed demo uses `model_tier = "cpu"`: smaller open-weight models (Qwen2.5-1.5B on llama.cpp, faster-whisper-base, Kokoro) on a c7i.2xlarge, behind the same chaos proxy, with a scaled-down campaign. Everything else is identical, and `model_tier = "gpu"` deploys the GPU design unchanged. Details, and what the CPU run does not show: [`docs/cpu-tier.md`](docs/cpu-tier.md).

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
                                  Speaches    :8000  faster-whisper (STT) + Kokoro (TTS) ◄────┘
                              cpu tier: c7i.2xlarge, AL2023, llama.cpp (Qwen2.5-1.5B Q4_K_M) in place of vLLM
 NAT gateway (1) for egress: image + model downloads; S3/DynamoDB gateway endpoints bypass it
```

With `model_tier = "mock"` the internal NLB fronts the Round 1 mock provider on Fargate instead of the model host. That is a cheap way to check all the infrastructure before paying for the models.

**Security posture of the demo.** The demo ALB is HTTP-only and unauthenticated. It is restricted by security group to `allowed_cidrs` (your own /32) and serves synthetic data only (generated utterances, no borrower data). The DLQ and admin endpoints are on that same listener. The production path is an HTTPS listener with an ACM certificate, service-to-service auth (SigV4 or mTLS) or a private ALB behind the telephony layer, and the DLQ and admin endpoints moved off the public listener.

### Prerequisites

- An AWS account. This one has no credits and a $20 hard spending cap.
- If the account sits in an AWS Organization, the organization's SCP must allow EC2, ECS, ELB, DynamoDB, CloudWatch, Logs, ECR, SSM and Auto Scaling in ap-southeast-2. Otherwise `apply` fails with access-denied errors that name no policy.
- For `model_tier = "cpu"`: 8 vCPUs of "Running On-Demand Standard" instances (`L-1216C47A`; new accounts get 8).
- For `model_tier = "gpu"`: a quota of at least 4 vCPUs for "Running On-Demand G and VT instances" (quota code `L-DB2E81BA`). New accounts start at 0. Request it on day one, because approval can take a day or more:

  ```bash
  aws service-quotas get-service-quota --service-code ec2 --quota-code L-DB2E81BA --profile predixion --region ap-southeast-2
  aws service-quotas request-service-quota-increase --service-code ec2 --quota-code L-DB2E81BA --desired-value 4 --profile predixion --region ap-southeast-2
  ```
- Docker Desktop, running, for every `terraform` command including `destroy`. Terraform builds and pushes the app image through the docker provider (`docker_host` defaults to the Windows named pipe; on Linux or macOS set `docker_host = "unix:///var/run/docker.sock"`).
- Terraform 1.9 or later, AWS CLI v2, and [uv](https://docs.astral.sh/uv/).

### One-time setup

**1. The `predixion` profile.** All commands use a profile of that name, scoped to this project:

```bash
aws configure --profile predixion          # access key, secret, default region ap-southeast-2, output json
aws sts get-caller-identity --profile predixion
```

**2. A budget.** A $20 monthly cost budget, with email alerts at $5, $10 and $15 of actual spend and at $18 forecast. It is a one-time CLI step, not part of the Terraform. Budgets is a global service served from us-east-1, and the email address is a placeholder.

```bash
aws budgets create-budget --account-id <ACCOUNT_ID> --profile predixion --region us-east-1 \
  --budget '{"BudgetName":"predixion-20usd-cap","BudgetLimit":{"Amount":"20","Unit":"USD"},"TimeUnit":"MONTHLY","BudgetType":"COST"}'
for t in 5 10 15; do
  aws budgets create-notification --account-id <ACCOUNT_ID> --budget-name predixion-20usd-cap --profile predixion --region us-east-1 \
    --notification NotificationType=ACTUAL,ComparisonOperator=GREATER_THAN,Threshold=$t,ThresholdType=ABSOLUTE_VALUE \
    --subscribers SubscriptionType=EMAIL,Address=you@example.com
done
aws budgets create-notification --account-id <ACCOUNT_ID> --budget-name predixion-20usd-cap --profile predixion --region us-east-1 \
  --notification NotificationType=FORECASTED,ComparisonOperator=GREATER_THAN,Threshold=18,ThresholdType=ABSOLUTE_VALUE \
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
| `model_tier` | `mock` | `mock` (Fargate mock provider), `gpu` (g5.xlarge with vLLM and Speaches) or `cpu` (c7i.2xlarge with llama.cpp and Speaches, see [`docs/cpu-tier.md`](docs/cpu-tier.md)) |
| `min_tasks` / `max_tasks` | 2 / 10 | Fargate task floor and ceiling |
| `target_active_calls` | 10 | Autoscaling target, active calls per task |
| `campaign_prewarm` | none | `{ start, end, min_tasks }`, `at(...)` or `cron(...)` in UTC. Use `min_tasks = 3`: pre-warm the floor, and let ActiveCalls target tracking scale the spike |
| `failure_rate` | 0.2 | Injected provider failure rate (changing it replaces the model host) |
| `baseline_p99_ms` | 3000 | The latency alarm fires above 3x this |
| `model_host_max_hours` | 3 | Dead-man switch: the model host scales to zero this long after creation |
| `stage_timeouts_s` / `turn_deadline_s` | per tier | 5/8/5 s and 20 s for `mock` and `gpu`; 8/15/8 s and 30 s for `cpu` |
| `docker_host` | Windows named pipe | Docker daemon address |

### Deploy

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
#    CPU (what the demo ran, docs/cpu-tier.md): model_tier = "cpu" and target_active_calls = 4.
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
uv run voice-agent campaign --url $URL --profile smoke:3:12
uv run voice-agent campaign --url $URL --profile baseline:10:4,spike:15:20,cooldown:10:4 --json-out results/campaign.json

# 7. While it runs (during the spike): chaos, then replay
uv run python scripts/chaos.py outage --stage llm --seconds 60
# Replay once every breaker is closed (poll `curl -s $URL/health`, or watch the BreakerState widget),
# in batches: the ALB closes a request after 60 s idle, so one big replay would return 504.
curl -s -X POST "$URL/v1/dlq/replay?limit=50"      # repeat until the next line shows 0 pending
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
- Operating it, and what changes are expensive to apply, is in [`RUNBOOK.md`](RUNBOOK.md).

### Cost

Estimated, not measured. ap-southeast-2 on-demand prices from the AWS Price List API, checked on 2026-10-08 and 2026-10-09. Both plans fit a **$20 hard cap** with no credits. The stack is up for about 5 hours in total: about 1 hour on the mock tier to check the infrastructure, then one model-host session in which the host runs about 3 hours and boots once. Fargate averages 5 tasks of 0.5 vCPU / 1 GB.

| Item | Unit price | Usage | CPU tier (deployed) | GPU tier (designed) |
|---|---|---|---|---|
| Model host | c7i.2xlarge $0.466/h; g5.xlarge $1.308/h | 3 h | $1.40 | $3.92 |
| NAT gateway, hourly | $0.059/h | 5 h | $0.30 | $0.30 |
| NAT gateway, data | $0.059/GB | images and model weights, 1 boot: ~6 GB (cpu), ~20 GB (gpu) | $0.35 | $1.18 |
| Fargate 0.5 vCPU / 1 GB | $0.0296/task-h ($0.04856/vCPU-h + $0.00532/GB-h) | ~25 task-h | $0.74 | $0.74 |
| CloudWatch (EMF metrics, Container Insights, logs) | $0.30/metric-month, prorated hourly | ~30 metrics, under 1 GB logs | ~$1.00 | ~$1.00 |
| ALB + internal NLB | $0.0252/h each + LCU | 5 h | ~$0.35 | ~$0.35 |
| Public IPv4 (NAT EIP + 2 ALB) | $0.005/h each | 5 h | $0.08 | $0.08 |
| EBS gp3 (host root: 40 GB cpu, 100 GB gpu) | $0.096/GB-month | 3 h | $0.02 | $0.04 |
| DynamoDB on-demand, ECR, data out, Budgets | | tiny | ~$0.05 | ~$0.05 |
| **Subtotal** | | | **~$4.30** | **~$7.70** |
| **With 30% contingency** | | | **~$5.60** | **~$10** |

The model host and the NAT data dominate. Each extra host hour costs about $0.50 (cpu) or $1.40 (gpu), counting EBS and NAT. The guards are:
- destroying the stack after every session;
- the dead-man switch on the model host (`model_host_max_hours`, default 3);
- an AWS Budget alerting at $5, $10 and $15 actual and at $18 forecast;
- the organization's Budgets spend-limit SCP.

Measured numbers from Cost Explorer and CloudWatch will replace these estimates after the demo run.

| | Estimate (CPU tier) | Measured (to be filled after the demo) |
|---|---|---|
| Total | ~$4.30 (~$5.60 with contingency) | |
| Model host hours | 3 | |
| NAT data | ~6 GB | |

### Design decisions, in short

1. **Model tier is EC2 with docker compose**, not ECS-on-EC2 or SageMaker (the `cpu` tier keeps the same shape; see [`docs/cpu-tier.md`](docs/cpu-tier.md)). Only one GPU fits the quota and three model servers must share it; ECS gives a GPU to one container exclusively, and SageMaker wants one GPU instance per endpoint. An ASG (min = max = 1) keeps the host self-healing and an internal NLB gives it a stable address.
2. **Fault injection is a proxy mode of the Round 1 mock** (`voice-agent chaos-proxy`). It applies the same 20% failure mix and forwards healthy requests to vLLM and Speaches by path. The service talks to it through the `openai` adapters, which is the Round 1 swap story.
3. **DynamoDB replaces SQLite**, because Fargate disks are per-task and a scale-in would lose dead letters. One table holds turns and dead letters; `TransactWriteItems` keeps "turn degraded plus dead letter written" atomic. SQS was rejected for the system of record because a send cannot join that transaction. Both store backends pass the same contract tests.
4. **Autoscaling is on ActiveCalls per task** (target 10, min 2, max 10), emitted as a CloudWatch EMF metric; ALB stickiness keeps each call on one task. A scheduled action pre-warms the floor before a known campaign window; the target-tracking policy does the scaling to the peak.
5. **Observability is EMF plus ALB and Container Insights metrics**, with one dashboard (traffic, capacity, dependencies) and alarms for p99 latency over 3x baseline, 5xx (target and ALB-generated), open breakers and DLQ growth. The EMF flush interval is 10 s (`EMF_INTERVAL_S`), deliberately short so the dashboard and the ActiveCalls scaling signal react within a minute, at the cost of a few more metric datapoints.
6. **IAM uses custom policies with explicit actions only.** `scripts/check_iam.py` fails on any `*` action or AWS-managed policy attachment; the checker has its own tests in `tests/scripts`.
7. **Cost guardrails:** the budget is a one-time CLI step (above, not in the Terraform); the model host dead-man switch is in the IaC, and `teardown_check.py` proves the destroy.
8. **State and secrets:** Terraform state is local and git-ignored (an S3 backend with a lockfile is the team setup); there are no secrets, as model servers need no keys and the deployer uses its own CLI profile.

### Deliverables

| Deliverable | Where |
|---|---|
| Terraform that builds and tears down everything | [`infra/`](infra); CI runs `terraform fmt -check`, `init -backend=false` and `validate` |
| Service changes for AWS (DynamoDB store, EMF metrics, call tracking, `campaign` command, chaos proxy) | [`src/voice_agent`](src/voice_agent), [`src/mock_provider`](src/mock_provider) |
| Scripts | [`scripts/chaos.py`](scripts/chaos.py), [`scripts/check_iam.py`](scripts/check_iam.py), [`scripts/teardown_check.py`](scripts/teardown_check.py), [`scripts/make_utterances.ps1`](scripts/make_utterances.ps1) |
| Runbook | [`RUNBOOK.md`](RUNBOOK.md) |
| 5-minute demo shot list | [`docs/demo-shot-list.md`](docs/demo-shot-list.md) |
| Region notes | [`docs/ap-south-1-notes.md`](docs/ap-south-1-notes.md) |
| Cost write-up | This section holds the estimate. The one-page PDF (Round 1 cost, and the swap-the-mock paragraph) is sent separately and is not in the repo, and Round 2's measured numbers are added after the demo |
| Teardown proof | `teardown/*.log`, written by `teardown_check.py` and committed after the destroy |
| Reimbursement | The billing PDF is supplied separately by the account owner |
