# Failure handling, dead letters and configuration

How the service keeps a call alive when a provider misbehaves. The [README](../README.md) has the short version.

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

## Retries, breakers and degradation

**Error taxonomy.** Adapters turn every failure into a `ProviderError` with a `kind`:

| Kind | Source | Retried | Counts toward breaker |
|---|---|:-:|:-:|
| `timeout`, `connection` | attempt timeout, connect/read errors, HTTP 408 | ✓ | ✓ |
| `rate_limited` | 429 (honours `Retry-After`) | ✓ | ✓ |
| `server_error` | 5xx | ✓ | ✓ |
| `malformed` | a 2xx with an unparseable or wrong-shaped body | ✓ | ✓ |
| `client_error`, `auth` | 400/404/422, 401/403 | ✗ | ✗ |

A 4xx means *our* request or configuration is wrong. The provider itself is healthy, so a 4xx never trips the breaker. Bugs in our own code are not `ProviderError`s, so they are never retried, but they are still dead-lettered.

**Retry** ([`retry.py`](../src/voice_agent/resilience/retry.py)):
- Up to 3 attempts. The delay is `uniform(0, min(2 s, 0.1 s · 2ⁿ))` (full jitter), so many calls failing together don't retry in lock-step.
- `Retry-After` is honoured, capped at the maximum delay.
- Retries stay inside the turn deadline: no backoff sleeps past it, no attempt starts with less than 50 ms left, and each attempt's timeout is `min(stage timeout, remaining budget)`. A hung provider costs at most one attempt timeout.

**Circuit breaker** ([`breaker.py`](../src/voice_agent/resilience/breaker.py)), one per stage:
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

**Degradation** ([`pipeline.py`](../src/voice_agent/pipeline.py)). A turn is never dropped, and it is always recorded:
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

**Replay semantics** ([`dlq.py`](../src/voice_agent/dlq.py)):
- **Claiming:** an entry is claimed atomically (`pending → replaying`), so two operators can never replay it at the same time.
- **Running:** the stored turn re-runs through the *same* resilient pipeline and the same breakers. A provider that is still down fails fast instead of being hammered.
- **Success:** the turn becomes `completed_on_replay`, with the transcript and reply recorded. The entry is marked `resolved`.
- **Failure:** the entry returns to `pending`, with `replay_count + 1` and the error recorded.
- **Open circuit:** while any circuit is open, a replay is `skipped` (HTTP 503) without being claimed or charged a `replay_count`. It would only fail fast.
- **Bulk replay:** it replays one entry at a time until one has *resolved* and every circuit is closed. That first success is the canary, needed because a fresh CLI process's breakers know nothing of an ongoing outage. If a replay is deferred or fails for any reason other than its own unusable payload, it stops and marks the rest `skipped` (untouched). After that it runs 8 entries at a time.
- **CLI safety:** the CLI's `dlq replay` is safe to run while the service is running. It never runs recovery, so it can't reclaim the service's in-flight turns, and atomic claims prevent double replays.
- **Purpose:** the live moment has passed, so a replay *completes the compliance record* (what the debtor said, what the agent would have said) and can drive a callback. It doesn't speak to the caller.

## Configuration

Everything is set through environment variables (or `.env`). See [`.env.example`](../.env.example) for the full list and its defaults. There are no secrets in the repo: API keys (`*_API_KEY`) are optional, have no default, and are held as `SecretStr` so they never appear in logs or reprs. A key is sent as `Authorization: Bearer` only when it is set. The mock reads `MOCK_FAILURE_RATE`, `MOCK_SEED`, `MOCK_LATENCY_SCALE` and `MOCK_HANG_S` from the process environment.

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
- **End-to-end tests** ([`tests/test_e2e.py`](../tests/test_e2e.py)): the real wiring runs against the real mock app in-process, and the assertions are made from the **provider's side**:
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

## Example local run

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
