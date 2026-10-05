# Resilient Collections Voice-Agent — Design

Date: 2026-10-05 · Status: approved (architecture), building

## Goal

A local collections voice-agent inference service that runs every caller turn through an
STT → LLM → TTS pipeline behind a resilience layer, so a flaky provider (20% injected failure)
degrades a call gracefully and **never drops or silently loses a turn**. Failed turns land in a
persistent, inspectable, replayable dead-letter queue. The provider boundary is model-agnostic:
the mock can be swapped for open-weight servers (Speaches/faster-whisper, vLLM/Ollama/llama.cpp,
Kokoro) by configuration alone. A reproducible cost model estimates AWS Fargate cost at two
scaling configurations.

## Non-goals

Real telephony / media streaming, auth on our own API, multi-node coordination of breaker state,
running GPU models in CI.

## Architecture

Two processes:

1. **voice-agent** (FastAPI) — orchestrator, resilience layer, SQLite event log + DLQ, HTTP API, CLI.
2. **mock-provider** (FastAPI) — stands in for STT/LLM/TTS. Lognormal latency per stage,
   `FAILURE_RATE=0.2`, mixed failure modes, runtime chaos controls and request stats.

```
POST /v1/calls/{call_id}/turns
  └─ TurnPipeline (turn deadline)
       ├─ write-ahead: turn row status=in_progress
       ├─ ResilientStage("stt") ─ Retry( Breaker( SttProvider.transcribe ) )
       ├─ ResilientStage("llm") ─ Retry( Breaker( LlmProvider.complete ) )
       ├─ ResilientStage("tts") ─ Retry( Breaker( TtsProvider.synthesize ) )
       ├─ ok       → turn status=completed
       └─ failure  → ONE transaction: turn status=degraded + dead_letters row
                     → respond with pre-rendered fallback prompt (no TTS dependency)
```

### Package layout

```
src/voice_agent/
  config.py              env-driven settings (pydantic-settings); secrets are SecretStr, never defaulted
  providers/base.py      Protocols (SttProvider, LlmProvider, TtsProvider) + ProviderError taxonomy
  providers/http.py      shared httpx → ProviderError classification
  providers/mock.py      adapters for the mock-provider JSON API
  providers/openai_compat.py  adapters for OpenAI-compatible open-weight servers
  providers/factory.py   build providers from config (the only place that knows adapter names)
  resilience/retry.py    RetryPolicy: exp backoff + full jitter, Retry-After, deadline-aware
  resilience/breaker.py  CircuitBreaker: closed/open/half-open, rolling count window
  resilience/stage.py    ResilientStage = per-attempt timeout + breaker + retry + attempt records
  pipeline.py            TurnPipeline: orchestrates one turn, degradation, handoff decision
  fallback.py            pre-rendered fallback prompts (audio does not depend on TTS)
  store.py               SQLite (aiosqlite, WAL): turns (audit log) + dead_letters
  dlq.py                 DeadLetterService: list / show / replay (claim → run → resolve)
  metrics.py             Prometheus counters/gauges
  api.py                 FastAPI app + lifespan (recovers interrupted turns on startup)
  cli.py                 serve | dlq list/show/replay | loadtest | chaos-demo
src/mock_provider/app.py
cost/fargate_cost.py     reproducible cost calculator
```

## Error taxonomy (the contract between adapters and resilience)

Adapters translate everything into `ProviderError(stage, kind, retryable, status_code, retry_after)`:

| Condition | retryable | counts toward breaker |
|---|---|---|
| timeout, connect/read error | yes | yes |
| 429, 500, 502, 503, 504 (honour `Retry-After`) | yes | yes |
| malformed/undecodable 2xx body | yes | yes |
| 400, 401, 403, 404, 422 (our request/config is wrong) | no | no |

The resilience layer only ever sees `ProviderError`; it never imports httpx or knows adapter names.

## Resilience semantics

**Order:** `Retry(Breaker(attempt))` — every attempt passes the breaker, so the breaker sees raw
provider health; an open breaker raises `CircuitOpenError`, which is *not* retried (fail fast).

**Retry:** `max_attempts=3`, delay = `uniform(0, min(max_delay, base * 2^n))` (full jitter,
base 0.1s, cap 2s). `Retry-After` is honoured (capped). Never sleeps past the turn deadline;
never starts an attempt with < 50 ms of budget left. Per-attempt timeout =
`min(stage_timeout, remaining_budget)`.

**Breaker:** count-based rolling window of the last 50 counted outcomes, `minimum_calls=20`,
trips (on a failure) at failure rate ≥ 60%. Chosen by simulating the breaker: at 50% it
false-tripped in 0.5% of 3,000-attempt runs at the baseline 20% flakiness (mostly during
warm-up); at 60% the false-trip risk is ~1e-4 per breaker warm-up and none were seen in
~30M steady-state attempts, and a real outage still trips it within 27 attempts.
Open for `open_seconds=5`, then half-open admits up to 5 probes, and the outcome is
decided by majority as results arrive (≥ 60% of the probe limit failing → open again, more than 40% succeeding → closed with the window reset). Probes beyond the limit are rejected.
One breaker per stage, so a TTS outage does not block STT.

## Degradation and compliance

- Write-ahead: the turn row is inserted (`in_progress`) before any provider call.
- On stage failure the turn is marked `degraded` and a dead-letter row is inserted **in the same
  transaction, before the HTTP response is sent**. Dead-letter rows carry the original request,
  failed stage, error kind, every attempt (number, error, status, duration, backoff), and partial
  results (e.g. transcript if STT succeeded).
- Response is HTTP 200 with `status="degraded"`, a pre-rendered fallback prompt, `dlq_id`, and
  `action`: `retry_prompt`, or `handoff` once a call has `HANDOFF_AFTER_DEGRADED=2` consecutive
  degraded turns (transfer to a human / schedule callback).
- Startup recovery: turns left `in_progress` by a crash are moved to `interrupted` and
  dead-lettered, so a process crash cannot lose a turn either. While running, a lease sweeper
  does the same for turns/replay claims untouched for `max(60s, 3 x turn deadline)`.
- Finalization (complete or dead-letter) runs shielded from cancellation; every state
  transition is guarded in SQL (`WHERE status = <expected>`); only the service runs recovery
  (the CLI never does).
- `/health` never depends on downstream health (a provider outage must not make the load balancer
  kill healthy orchestrator tasks); it reports breaker states for humans.

## Dead-letter queue

SQLite table `dead_letters`: `id, call_id, turn_id, failed_stage, error_kind, error_detail,
payload_json, attempts_json, partial_json, status(pending|replaying|resolved), replay_count,
last_replay_error, created_at, last_replay_at, resolved_at`, unique on `(call_id, turn_id)`.

Replay = claim (`UPDATE … SET status='replaying' WHERE id=? AND status='pending'`, atomic) →
re-run the stored turn through the same resilient pipeline → on success the turn becomes
`completed_on_replay` with transcript/reply recorded and the DLQ row `resolved`; on failure the
row returns to `pending` with `replay_count+1` and the error. Replays never create new DLQ rows.
Purpose: the live moment has passed, but the compliance record (what the debtor said, what the
agent would have said) is completed and can drive a follow-up.

Interfaces: `GET /v1/dlq`, `GET /v1/dlq/{id}`, `POST /v1/dlq/{id}/replay`, `POST /v1/dlq/replay`
(bulk pending), and CLI `voice-agent dlq list|show|replay`. The DB file is plain SQLite and can
be opened with any SQLite client.

## Model-agnostic boundary

Each stage is a `typing.Protocol` with one async method. `providers/factory.py` maps
`{STT,LLM,TTS}_PROVIDER = mock | openai` to adapters. The `openai` adapters target the
OpenAI-compatible APIs exposed by open-weight servers:
STT `POST /v1/audio/transcriptions` (Speaches), LLM `POST /v1/chat/completions`
(vLLM/Ollama/llama.cpp), TTS `POST /v1/audio/speech` (Kokoro-FastAPI/Speaches). Optional API key
from env as Bearer. Stages can be mixed (e.g. real LLM, mock STT/TTS).

## Mock provider

`POST /v1/stt {audio_b64}`, `/v1/llm {messages}`, `/v1/tts {text}`. Per-stage lognormal latency
(medians: STT 150 ms, LLM 400 ms, TTS 200 ms). Failures with probability `FAILURE_RATE`
independently per request; mode mix 503 40%, 500 20%, 429+Retry-After 15%, hang past timeout 15%,
malformed 200 10%. `MOCK_SEED` for determinism. `POST /admin/chaos` changes failure rate per
stage or forces an outage for N seconds; `GET /admin/stats` returns per-stage request/outcome
counts so retries and breaker fail-fast are visible from the downstream side.

## Testing

pytest + pytest-asyncio; fake clock/sleep/RNG for deterministic resilience tests;
`httpx.MockTransport` for adapter contract tests; `httpx.ASGITransport` wires the real mock
provider app in-process for end-to-end tests (no ports). Key end-to-end properties:
(1) under 20% failure, every turn ends `completed` or `degraded`+DLQ (zero unaccounted turns),
and the mock sees more requests than turns×stages (retries happen);
(2) during a forced outage the breaker opens and downstream request count stops growing;
after the outage it half-opens and closes; (3) DLQ replay resolves entries once providers recover.
ruff + mypy + GitHub Actions CI.

## Cost model

`cost/fargate_cost.py` (us-east-1, Linux/x86 on-demand: $0.04048/vCPU-h, $0.004445/GB-h,
checked 2026-10-05) computes two configurations — (A) static fleet sized for peak, (B) baseline
fleet + scheduled pre-warm scale-out for the daily campaign window + target tracking — from
explicit assumptions (call duration, concurrent calls per task, utilisation target, N+1), with a
sensitivity table and ARM/Spot notes. Write-up in `docs/COST.md`.
